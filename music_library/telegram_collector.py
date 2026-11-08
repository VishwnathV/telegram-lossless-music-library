from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import FileReferenceExpiredError
from telethon.tl.custom.message import Message
from telethon.tl.types import InputDocumentFileLocation

from .catalog import ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS, Cataloger, safe_filename
from .config import Settings
from .database import Database

logger = logging.getLogger(__name__)


class TelegramCollector:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        if not settings.tg_api_id or not settings.tg_api_hash:
            raise ValueError("TG_API_ID and TG_API_HASH must be configured")
        self.settings = settings
        self.database = database
        self.cataloger = Cataloger(settings, database)
        self.on_event = on_event or (lambda _event, _payload: None)
        self.should_stop = should_stop or (lambda: False)
        self.client = TelegramClient(
            str(settings.tg_session_path), settings.tg_api_id, settings.tg_api_hash
        )

    async def authorize(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        logger.info("Authorized as %s", getattr(me, "username", None) or me.id)

    async def sync(self, references: Sequence[str | int] | None = None) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise ValueError("Telegram session is not authorized; connect the account in the dashboard")
        try:
            selected = list(references if references is not None else self.settings.sources)
            self.on_event("inventory_start", {"groups_total": len(selected)})
            work: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def inventory_sources() -> None:
                try:
                    for reference in selected:
                        if self.should_stop():
                            break
                        await self._inventory_source(reference, work)
                    if not self.should_stop():
                        self.on_event("inventory_ready", {})
                finally:
                    await work.put(None)

            async def download_sources() -> None:
                blocked_sources: set[int] = set()
                while True:
                    item = await work.get()
                    if item is None:
                        break
                    if self.should_stop():
                        break
                    source_id = int(item["source_id"])
                    if item["kind"] == "source_complete":
                        if source_id not in blocked_sources and item["scan_complete"]:
                            final_message_id = int(item["final_message_id"])
                            if final_message_id > int(item["last_message_id"]):
                                with self.database.connect() as connection:
                                    connection.execute(
                                        """UPDATE sources SET last_message_id=?,
                                        updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                        (final_message_id, source_id),
                                    )
                        continue
                    message: Message = item["message"]
                    title = str(item["title"])
                    succeeded = await self._process_message(source_id, message, title)
                    if not succeeded:
                        blocked_sources.add(source_id)
                        continue
                    # Once a source has a gap, keep its cursor before that gap. Later files may
                    # still be imported, but the next sync must revisit the failed message.
                    if source_id not in blocked_sources:
                        with self.database.connect() as connection:
                            connection.execute(
                                """UPDATE sources SET last_message_id=?,
                                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                (message.id, source_id),
                            )
                    self.on_event(
                        "inventory_processed",
                        {"size": int(item["size"]), "source": title, "source_id": source_id},
                    )

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(inventory_sources())
                tasks.create_task(download_sources())
        finally:
            await self.client.disconnect()

    async def _inventory_source(
        self, reference: str | int, work: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        entity = await self.client.get_entity(reference)
        # Marked peer IDs keep users, groups, and channels unambiguous across sessions.
        telegram_id = int(utils.get_peer_id(entity))
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(telegram_id)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO sources(telegram_id,title) VALUES (?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET title=excluded.title,updated_at=CURRENT_TIMESTAMP""",
                (telegram_id, title),
            )
            source = connection.execute(
                "SELECT id,last_message_id FROM sources WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        source_id = int(source["id"])
        last_message_id = int(source["last_message_id"])
        logger.info("Inventorying %s after message %s", title, last_message_id)
        self.on_event("inventory_source", {"source": title, "source_id": source_id})

        final_message_id = last_message_id
        async for message in self.client.iter_messages(entity, min_id=last_message_id, reverse=True):
            if self.should_stop():
                break
            final_message_id = max(final_message_id, int(message.id))
            file = message.file
            name = getattr(file, "name", None) if file else None
            suffix = Path(name or "").suffix.lower()
            size = int(getattr(file, "size", 0) or 0) if file else 0
            if not file or suffix not in AUDIO_EXTENSIONS | ARCHIVE_EXTENSIONS:
                continue
            if size > self.settings.max_download_bytes:
                logger.warning("Skipping oversized file %s (%s bytes)", name, size)
                continue
            existing = self.database.one(
                "SELECT status FROM messages WHERE source_id=? AND telegram_message_id=?",
                (source_id, message.id),
            )
            if existing and existing["status"] == "complete":
                continue
            self.on_event(
                "inventory_discovered",
                {
                    "filename": name,
                    "size": size,
                    "source": title,
                    "source_id": source_id,
                    "is_archive": suffix in ARCHIVE_EXTENSIONS,
                    "existing": existing is not None,
                },
            )
            await work.put(
                {
                    "kind": "media",
                    "source_id": source_id,
                    "title": title,
                    "message": message,
                    "size": size,
                }
            )

        scan_complete = not self.should_stop()
        if scan_complete:
            self.on_event("inventory_group", {"source": title, "source_id": source_id})
        await work.put(
            {
                "kind": "source_complete",
                "source_id": source_id,
                "last_message_id": last_message_id,
                "final_message_id": final_message_id,
                "scan_complete": scan_complete,
            }
        )

    async def _refresh_message_reference(self, message: Message) -> Message:
        """Fetch the message again so Telegram returns a current file reference."""
        input_chat = getattr(message, "input_chat", None) or getattr(message, "peer_id", None)
        if input_chat is None:
            get_input_chat = getattr(message, "get_input_chat", None)
            if get_input_chat is not None:
                input_chat = await get_input_chat()
        if input_chat is None:
            raise RuntimeError(f"Cannot refresh Telegram message {message.id}: source is unknown")

        refreshed = await self.client.get_messages(input_chat, ids=int(message.id))
        if isinstance(refreshed, (list, tuple)):
            refreshed = refreshed[0] if refreshed else None
        if refreshed is None or getattr(refreshed, "document", None) is None:
            raise RuntimeError(f"Telegram media is no longer available for message {message.id}")
        return refreshed

    async def _download_document(
        self,
        message: Message,
        download_path: Path,
        part_size_kb: int,
        size: int,
        progress: Callable[[int, int], None],
    ) -> None:
        document = message.document
        location = InputDocumentFileLocation(
            id=document.id,
            access_hash=document.access_hash,
            file_reference=document.file_reference,
            thumb_size="",
        )
        await self.client.download_file(
            location,
            str(download_path),
            part_size_kb=part_size_kb,
            file_size=size,
            progress_callback=progress,
            dc_id=document.dc_id,
        )

    async def _download_document_with_refresh(
        self,
        message: Message,
        download_path: Path,
        part_size_kb: int,
        size: int,
        progress: Callable[[int, int], None],
    ) -> None:
        try:
            await self._download_document(message, download_path, part_size_kb, size, progress)
        except FileReferenceExpiredError:
            logger.info("Refreshing expired Telegram reference for message %s", message.id)
            download_path.unlink(missing_ok=True)
            progress(0, size)
            refreshed = await self._refresh_message_reference(message)
            await self._download_document(refreshed, download_path, part_size_kb, size, progress)

    def _start_archive_job(
        self,
        message_record_id: int,
        filename: str,
        compressed_size: int,
        source_id: int,
        source_title: str | None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO archive_jobs(
                    message_id,filename,status,compressed_size,started_at
                ) VALUES (?,?, 'downloading',?,CURRENT_TIMESTAMP)
                ON CONFLICT(message_id) DO UPDATE SET
                    filename=excluded.filename,status='downloading',
                    compressed_size=excluded.compressed_size,expanded_size=0,
                    bytes_extracted=0,entries_total=0,entries_processed=0,
                    analyzed_entries=0,audio_entries=0,ignored_entries=0,
                    tracks_imported=0,duplicates=0,current_entry=NULL,error=NULL,
                    started_at=CURRENT_TIMESTAMP,finished_at=NULL,
                    updated_at=CURRENT_TIMESTAMP""",
                (message_record_id, filename, compressed_size),
            )
        self.on_event(
            "archive_started",
            {
                "message_id": message_record_id,
                "filename": filename,
                "compressed_size": compressed_size,
                "source_id": source_id,
                "source": source_title,
            },
        )

    def _archive_event(
        self,
        message_record_id: int,
        event: str,
        payload: dict[str, Any],
        filename: str,
        source_id: int,
        source_title: str | None,
        *,
        persist: bool = True,
    ) -> None:
        if persist:
            with self.database.connect() as connection:
                if event == "archive_validating":
                    connection.execute(
                        """UPDATE archive_jobs SET status='validating',current_entry=NULL,
                        updated_at=CURRENT_TIMESTAMP WHERE message_id=?""",
                        (message_record_id,),
                    )
                elif event == "archive_validated":
                    connection.execute(
                        """UPDATE archive_jobs SET status='extracting',expanded_size=?,
                        entries_total=?,audio_entries=?,ignored_entries=?,
                        updated_at=CURRENT_TIMESTAMP WHERE message_id=?""",
                        (
                            int(payload.get("expanded_size") or 0),
                            int(payload.get("entries_total") or 0),
                            int(payload.get("audio_entries") or 0),
                            int(payload.get("ignored_entries") or 0),
                            message_record_id,
                        ),
                    )
                elif event in {"archive_extracting", "archive_extracted"}:
                    connection.execute(
                        """UPDATE archive_jobs SET status='extracting',entries_processed=?,
                        bytes_extracted=?,current_entry=?,updated_at=CURRENT_TIMESTAMP
                        WHERE message_id=?""",
                        (
                            int(payload.get("entries_processed") or 0),
                            int(payload.get("bytes_extracted") or 0),
                            payload.get("current_entry"),
                            message_record_id,
                        ),
                    )
                elif event == "archive_analyzing":
                    connection.execute(
                        """UPDATE archive_jobs SET status='analyzing',analyzed_entries=?,
                        tracks_imported=?,duplicates=?,current_entry=?,
                        updated_at=CURRENT_TIMESTAMP WHERE message_id=?""",
                        (
                            int(payload.get("analyzed_entries") or 0),
                            int(payload.get("tracks_imported") or 0),
                            int(payload.get("duplicates") or 0),
                            payload.get("current_entry"),
                            message_record_id,
                        ),
                    )
                elif event == "archive_complete":
                    connection.execute(
                        """UPDATE archive_jobs SET status='complete',analyzed_entries=?,
                        tracks_imported=?,duplicates=?,current_entry=NULL,error=NULL,
                        finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                        WHERE message_id=?""",
                        (
                            int(payload.get("analyzed_entries") or 0),
                            int(payload.get("tracks_imported") or 0),
                            int(payload.get("duplicates") or 0),
                            message_record_id,
                        ),
                    )
                elif event == "archive_failed":
                    connection.execute(
                        """UPDATE archive_jobs SET status='failed',error=?,
                        finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                        WHERE message_id=?""",
                        (str(payload.get("error") or "Archive processing failed")[:1000], message_record_id),
                    )
        self.on_event(
            event,
            {
                **payload,
                "message_id": message_record_id,
                "filename": filename,
                "source_id": source_id,
                "source": source_title,
            },
        )

    async def _process_message(
        self, source_id: int, message: Message, source_title: str | None = None
    ) -> bool:
        file = message.file
        name = getattr(file, "name", None) if file else None
        suffix = Path(name or "").suffix.lower()
        if not file or suffix not in AUDIO_EXTENSIONS | ARCHIVE_EXTENSIONS:
            return True
        size = int(getattr(file, "size", 0) or 0)
        if size > self.settings.max_download_bytes:
            logger.warning("Skipping oversized file %s (%s bytes)", name, size)
            return True

        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO messages(source_id,telegram_message_id,telegram_date,original_name)
                VALUES (?,?,?,?) ON CONFLICT(source_id,telegram_message_id) DO NOTHING""",
                (source_id, message.id, message.date.isoformat(), name),
            )
            record = connection.execute(
                "SELECT id,status FROM messages WHERE source_id=? AND telegram_message_id=?",
                (source_id, message.id),
            ).fetchone()
        if record["status"] == "complete":
            return True

        message_record_id = int(record["id"])
        work_dir = self.settings.incoming_dir / f"{source_id}-{message.id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        local_name = safe_filename(name, f"telegram-{message.id}")
        download_path = work_dir / local_name
        is_archive = suffix in ARCHIVE_EXTENSIONS
        if is_archive:
            self._start_archive_job(
                message_record_id, name or download_path.name, size, source_id, source_title
            )
        try:
            self.on_event(
                "download",
                {
                    "filename": name or download_path.name,
                    "downloaded": 0,
                    "total": size,
                    "source": source_title,
                    "source_id": source_id,
                },
            )

            def progress(downloaded: int, total: int) -> None:
                self.on_event(
                    "download",
                    {
                        "filename": name or download_path.name,
                        "downloaded": downloaded,
                        "total": total,
                        "source": source_title,
                        "source_id": source_id,
                    },
                )

            document = message.document
            if document:
                # Telegram permits 512 KB download parts. Large parts substantially reduce
                # request/response latency for lossless albums and archives.
                part_size_kb = min(max(self.settings.telegram_download_part_kb, 4), 512)
                try:
                    await self._download_document_with_refresh(
                        message, download_path, part_size_kb, size, progress
                    )
                except Exception:
                    fallback_part_kb = min(part_size_kb, 128)
                    logger.warning(
                        "Fast Telegram download failed for %s; retrying with %s KB parts",
                        name,
                        fallback_part_kb,
                        exc_info=True,
                    )
                    download_path.unlink(missing_ok=True)
                    progress(0, size)
                    await self._download_document_with_refresh(
                        message, download_path, fallback_part_kb, size, progress
                    )
            else:
                await message.download_media(file=str(download_path), progress_callback=progress)
            self.on_event(
                "processing",
                {
                    "filename": name or download_path.name,
                    "source": source_title,
                    "source_id": source_id,
                },
            )
            last_archive_persist = 0.0

            def archive_progress(event: str, payload: dict[str, int | str]) -> None:
                nonlocal last_archive_persist
                now = time.monotonic()
                persist = (
                    event != "archive_extracting"
                    or now - last_archive_persist >= 0.5
                    or int(payload.get("entries_processed") or 0)
                    >= int(payload.get("audio_entries") or 0)
                )
                if persist:
                    last_archive_persist = now
                self._archive_event(
                    message_record_id,
                    event,
                    dict(payload),
                    name or download_path.name,
                    source_id,
                    source_title,
                    persist=persist,
                )

            if is_archive:
                self._archive_event(
                    message_record_id,
                    "archive_validating",
                    {},
                    name or download_path.name,
                    source_id,
                    source_title,
                )
            tracks = self.cataloger.expand(
                download_path,
                work_dir / "extracted",
                on_progress=archive_progress if is_archive else None,
            )
            imported = 0
            duplicates = 0
            last_analysis_persist = 0.0
            for index, track in enumerate(tracks, start=1):
                _, created = self.cataloger.ingest(track, message_record_id)
                imported += int(created)
                duplicates += int(not created)
                if is_archive:
                    now = time.monotonic()
                    persist = now - last_analysis_persist >= 0.5 or index == len(tracks)
                    if persist:
                        last_analysis_persist = now
                    self._archive_event(
                        message_record_id,
                        "archive_analyzing",
                        {
                            "analyzed_entries": index,
                            "audio_entries": len(tracks),
                            "tracks_imported": imported,
                            "duplicates": duplicates,
                            "current_entry": track.name,
                        },
                        name or download_path.name,
                        source_id,
                        source_title,
                        persist=persist,
                    )
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET status='complete',error=NULL WHERE id=?",
                    (message_record_id,),
                )
            if is_archive:
                self._archive_event(
                    message_record_id,
                    "archive_complete",
                    {
                        "analyzed_entries": len(tracks),
                        "audio_entries": len(tracks),
                        "tracks_imported": imported,
                        "duplicates": duplicates,
                    },
                    name or download_path.name,
                    source_id,
                    source_title,
                )
            logger.info("Processed %s: %s new tracks", name, imported)
            self.on_event(
                "imported",
                {
                    "filename": name or download_path.name,
                    "tracks": imported,
                    "source": source_title,
                    "source_id": source_id,
                },
            )
            return True
        except Exception as exc:
            logger.exception("Failed to process %s", name)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET status='failed',error=? WHERE id=?",
                    (str(exc)[:1000], message_record_id),
                )
            if is_archive:
                self._archive_event(
                    message_record_id,
                    "archive_failed",
                    {"error": str(exc)},
                    name or download_path.name,
                    source_id,
                    source_title,
                )
            self.on_event(
                "failed",
                {
                    "filename": name or download_path.name,
                    "error": str(exc),
                    "source": source_title,
                    "source_id": source_id,
                },
            )
            return False
        finally:
            if not self.settings.keep_incoming:
                shutil.rmtree(work_dir, ignore_errors=True)
