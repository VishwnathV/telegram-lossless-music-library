from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient
from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

from .config import Settings
from .database import Database
from .telegram_collector import TelegramCollector

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TelegramDashboard:
    """Browser-driven Telegram setup without exposing session credentials to the frontend."""

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._pending_client: TelegramClient | None = None
        self._pending_phone: str | None = None
        self._phone_code_hash: str | None = None
        self._lock = asyncio.Lock()
        self._last_status: dict[str, Any] = {
            "configured": False,
            "connected": False,
            "account": None,
        }

    def config(self) -> dict[str, Any]:
        api_id = self.database.setting("tg_api_id") or self.settings.tg_api_id
        api_hash = self.database.setting("tg_api_hash") or self.settings.tg_api_hash
        return {
            "api_id": api_id,
            "api_hash_configured": bool(api_hash),
            "session_path": str(self.settings.tg_session_path),
        }

    def save_config(self, api_id: int, api_hash: str) -> None:
        self.database.set_setting("tg_api_id", str(api_id))
        self.database.set_setting("tg_api_hash", api_hash.strip())

    def credentials(self) -> tuple[int, str]:
        raw_id = self.database.setting("tg_api_id") or self.settings.tg_api_id
        api_hash = self.database.setting("tg_api_hash") or self.settings.tg_api_hash
        if not raw_id or not api_hash:
            raise ValueError("Telegram API ID and API hash are not configured")
        return int(raw_id), str(api_hash)

    def runtime_settings(self) -> Settings:
        api_id, api_hash = self.credentials()
        return self.settings.model_copy(update={"tg_api_id": api_id, "tg_api_hash": api_hash})

    def client(self) -> TelegramClient:
        api_id, api_hash = self.credentials()
        return TelegramClient(str(self.settings.tg_session_path), api_id, api_hash)

    async def status(self) -> dict[str, Any]:
        try:
            client = self.client()
        except ValueError:
            self._last_status = {"configured": False, "connected": False, "account": None}
            return dict(self._last_status)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self._last_status = {"configured": True, "connected": False, "account": None}
                return dict(self._last_status)
            me = await client.get_me()
            self._last_status = {
                "configured": True,
                "connected": True,
                "account": {
                    "id": me.id,
                    "name": " ".join(part for part in (me.first_name, me.last_name) if part),
                    "username": me.username,
                    "phone": f"+{me.phone}" if me.phone else None,
                },
            }
            return dict(self._last_status)
        except Exception as exc:  # noqa: BLE001 - connection failures are dashboard state
            self._last_status = {
                "configured": True,
                "connected": False,
                "account": None,
                "error": str(exc),
            }
            return dict(self._last_status)
        finally:
            if "client" in locals():
                try:
                    await client.disconnect()
                except Exception:
                    logger.debug("Telegram status client did not disconnect cleanly", exc_info=True)

    def cached_status(self) -> dict[str, Any]:
        return dict(self._last_status)

    async def send_code(self, phone: str) -> dict[str, Any]:
        async with self._lock:
            await self._discard_pending()
            client = self.client()
            await client.connect()
            try:
                result = await client.send_code_request(phone)
            except Exception:
                await client.disconnect()
                raise
            self._pending_client = client
            self._pending_phone = phone
            self._phone_code_hash = result.phone_code_hash
            return {"sent": True, "phone": phone}

    async def verify(self, code: str | None, password: str | None) -> dict[str, Any]:
        async with self._lock:
            client = self._pending_client
            if client is None:
                raise ValueError("Request a Telegram verification code first")
            try:
                if password:
                    await client.sign_in(password=password)
                else:
                    await client.sign_in(
                        phone=self._pending_phone,
                        code=code,
                        phone_code_hash=self._phone_code_hash,
                    )
            except SessionPasswordNeededError:
                return {"connected": False, "needs_password": True}
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
                raise ValueError(str(exc)) from exc
            me = await client.get_me()
            self._last_status = {
                "configured": True,
                "connected": True,
                "account": {
                    "id": me.id,
                    "name": " ".join(part for part in (me.first_name, me.last_name) if part),
                    "username": me.username,
                    "phone": f"+{me.phone}" if me.phone else None,
                },
            }
            await self._discard_pending()
            return {
                "connected": True,
                "needs_password": False,
                "account": {"id": me.id, "name": me.first_name, "username": me.username},
            }

    async def _discard_pending(self) -> None:
        if self._pending_client:
            await self._pending_client.disconnect()
        self._pending_client = None
        self._pending_phone = None
        self._phone_code_hash = None

    async def logout(self) -> None:
        client = self.client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await client.disconnect()

    async def discover_groups(self) -> list[dict[str, Any]]:
        client = self.client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise ValueError("Connect a Telegram account first")
            groups: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_channel):
                    continue
                entity = dialog.entity
                kind = "channel" if getattr(entity, "broadcast", False) else "group"
                item = {
                    "telegram_id": int(dialog.id),
                    "title": dialog.name or str(entity.id),
                    "username": getattr(entity, "username", None),
                    "kind": kind,
                    "member_count": getattr(entity, "participants_count", None),
                }
                groups.append(item)
                with self.database.connect() as connection:
                    connection.execute(
                        """INSERT INTO sources(telegram_id,title,username,kind,enabled,member_count)
                        VALUES (?,?,?,?,0,?) ON CONFLICT(telegram_id) DO UPDATE SET
                        title=excluded.title,username=excluded.username,kind=excluded.kind,
                        member_count=excluded.member_count,updated_at=CURRENT_TIMESTAMP""",
                        (
                            item["telegram_id"],
                            item["title"],
                            item["username"],
                            item["kind"],
                            item["member_count"],
                        ),
                    )
            return groups
        finally:
            await client.disconnect()


class SyncManager:
    def __init__(
        self, settings: Settings, database: Database, telegram: TelegramDashboard
    ) -> None:
        self.settings = settings
        self.database = database
        self.telegram = telegram
        self.task: asyncio.Task[None] | None = None
        self.stop_requested = False
        self._inventory_completed_bytes = 0
        self.state: dict[str, Any] = {
            "status": "idle",
            "phase": "Ready",
            "current_source": None,
            "current_file": None,
            "downloaded": 0,
            "total": 0,
            "progress": 0,
            "imported": 0,
            "failed": 0,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "inventory": self._empty_inventory(),
            "archive": self._archive_summary(),
        }

    @staticmethod
    def _empty_inventory() -> dict[str, Any]:
        return {
            "status": "idle",
            "exact": False,
            "previously_completed": 0,
            "files_total": 0,
            "files_processed": 0,
            "files_remaining": 0,
            "bytes_total": 0,
            "bytes_processed": 0,
            "bytes_remaining": 0,
            "groups_total": 0,
            "groups_scanned": 0,
            "progress": 0,
            "groups": {},
        }

    def _archive_summary(self) -> dict[str, Any]:
        totals = self.database.one(
            """SELECT COUNT(1) AS total,
            COALESCE(SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END),0) AS processed,
            COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed,
            COALESCE(SUM(CASE WHEN status='complete' THEN compressed_size ELSE 0 END),0)
                AS compressed_size,
            COALESCE(SUM(CASE WHEN status='complete' THEN expanded_size ELSE 0 END),0)
                AS expanded_size,
            COALESCE(SUM(CASE WHEN status='complete' THEN entries_total ELSE 0 END),0)
                AS entries_total,
            COALESCE(SUM(CASE WHEN status='complete' THEN audio_entries ELSE 0 END),0)
                AS audio_entries,
            COALESCE(SUM(CASE WHEN status='complete' THEN ignored_entries ELSE 0 END),0)
                AS ignored_entries,
            COALESCE(SUM(CASE WHEN status='complete' THEN tracks_imported ELSE 0 END),0)
                AS tracks_imported,
            COALESCE(SUM(CASE WHEN status='complete' THEN duplicates ELSE 0 END),0)
                AS duplicates
            FROM archive_jobs"""
        ) or {}
        latest = self.database.one(
            """SELECT filename,status,compressed_size,expanded_size,bytes_extracted,
            entries_total,entries_processed,analyzed_entries,audio_entries,ignored_entries,
            tracks_imported,duplicates,current_entry,error
            FROM archive_jobs ORDER BY updated_at DESC,id DESC LIMIT 1"""
        )
        total = int(totals.get("total") or 0)
        processed = int(totals.get("processed") or 0)
        return {
            "exact": False,
            "total": total,
            "processed": processed,
            "pending": max(0, total - processed),
            "failed": int(totals.get("failed") or 0),
            "compressed_size": int(totals.get("compressed_size") or 0),
            "expanded_size": int(totals.get("expanded_size") or 0),
            "entries_total": int(totals.get("entries_total") or 0),
            "audio_entries": int(totals.get("audio_entries") or 0),
            "ignored_entries": int(totals.get("ignored_entries") or 0),
            "tracks_imported": int(totals.get("tracks_imported") or 0),
            "duplicates": int(totals.get("duplicates") or 0),
            "current": dict(latest) if latest else None,
        }

    def _update_archive_state(self, event: str, payload: dict[str, Any]) -> None:
        archive = dict(self.state.get("archive") or self._archive_summary())
        current = dict(archive.get("current") or {})
        current.update(
            {
                key: value
                for key, value in payload.items()
                if key
                in {
                    "filename",
                    "compressed_size",
                    "expanded_size",
                    "bytes_extracted",
                    "entries_total",
                    "entries_processed",
                    "analyzed_entries",
                    "audio_entries",
                    "ignored_entries",
                    "tracks_imported",
                    "duplicates",
                    "current_entry",
                    "error",
                }
            }
        )
        stages = {
            "archive_started": "downloading",
            "archive_validating": "validating",
            "archive_validated": "extracting",
            "archive_extracting": "extracting",
            "archive_extracted": "extracting",
            "archive_analyzing": "analyzing",
            "archive_complete": "complete",
            "archive_failed": "failed",
        }
        current["status"] = stages[event]
        if event == "archive_started":
            current.update(
                {
                    "expanded_size": 0,
                    "bytes_extracted": 0,
                    "entries_total": 0,
                    "entries_processed": 0,
                    "analyzed_entries": 0,
                    "audio_entries": 0,
                    "ignored_entries": 0,
                    "tracks_imported": 0,
                    "duplicates": 0,
                    "current_entry": None,
                    "error": None,
                    "progress": 0,
                }
            )
        elif event in {"archive_validated", "archive_extracting", "archive_extracted"}:
            total = int(current.get("audio_entries") or 0)
            done = int(current.get("entries_processed") or 0)
            current["progress"] = round(done / total * 100, 1) if total else 100
        elif event == "archive_analyzing":
            total = int(current.get("audio_entries") or 0)
            done = int(current.get("analyzed_entries") or 0)
            current["progress"] = round(done / total * 100, 1) if total else 100
        elif event in {"archive_complete", "archive_failed"}:
            current["progress"] = 100 if event == "archive_complete" else current.get("progress", 0)

        if event in {"archive_complete", "archive_failed"}:
            refreshed = self._archive_summary()
            refreshed["total"] = max(int(archive.get("total") or 0), int(refreshed["total"]))
            refreshed["pending"] = max(0, refreshed["total"] - refreshed["processed"])
            refreshed["exact"] = bool(archive.get("exact"))
            refreshed["current"] = current
            archive = refreshed
        else:
            archive["current"] = current
        self.state["archive"] = archive

        phase = {
            "archive_validating": "Validating ZIP archive",
            "archive_validated": "Preparing ZIP extraction",
            "archive_extracting": "Extracting ZIP audio",
            "archive_extracted": "ZIP extraction complete",
            "archive_analyzing": "Analyzing extracted tracks",
            "archive_complete": "Archive added to library",
            "archive_failed": "Archive needs attention",
        }.get(event)
        if phase:
            self.state["phase"] = phase

    def snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.state)
        inventory = dict(self.state["inventory"])
        inventory["groups"] = [
            dict(group) for group in inventory.get("groups", {}).values()
        ]
        snapshot["inventory"] = inventory
        archive = dict(self.state.get("archive") or {})
        if archive.get("current"):
            archive["current"] = dict(archive["current"])
        snapshot["archive"] = archive
        return snapshot

    @staticmethod
    def _group_for_event(
        inventory: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], str, dict[str, Any]]:
        source_id = str(int(payload["source_id"]))
        groups = dict(inventory.get("groups", {}))
        group = dict(groups[source_id])
        return groups, source_id, group

    async def start(self) -> dict[str, Any]:
        if self.task and not self.task.done():
            return self.snapshot()
        sources = self.database.all(
            """SELECT telegram_id,title,
            (SELECT MAX(messages.id) FROM messages
                WHERE messages.source_id=sources.id AND messages.status='failed') AS failed_id
            FROM sources WHERE enabled=1
            ORDER BY failed_id IS NULL,failed_id DESC,title COLLATE NOCASE"""
        )
        if not sources:
            raise ValueError("Select at least one Telegram group before starting sync")
        connection = await self.telegram.status()
        if not connection.get("connected"):
            raise ValueError("Connect a Telegram account before starting sync")
        self.stop_requested = False
        self._inventory_completed_bytes = 0
        self.state.update(
            {
                "status": "running",
                "phase": "Starting collector",
                "current_source": None,
                "current_file": None,
                "downloaded": 0,
                "total": 0,
                "progress": 0,
                "imported": 0,
                "failed": 0,
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
                "inventory": self._empty_inventory(),
                "archive": self._archive_summary(),
            }
        )
        references = [int(source["telegram_id"]) for source in sources]
        self.task = asyncio.create_task(self._run(references))
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        if self.state["status"] == "running":
            self.stop_requested = True
            self.state.update({"status": "stopping", "phase": "Stopping safely"})
        return self.snapshot()

    async def _run(self, references: list[int]) -> None:
        try:
            collector = TelegramCollector(
                self.telegram.runtime_settings(),
                self.database,
                on_event=self._event,
                should_stop=lambda: self.stop_requested,
            )
            await collector.sync(references)
            inventory = dict(self.state["inventory"])
            if not self.stop_requested and inventory["files_remaining"] == 0:
                inventory.update(
                    {
                        "status": "complete",
                        "exact": True,
                        "files_processed": inventory["files_total"],
                        "files_remaining": 0,
                        "bytes_processed": inventory["bytes_total"],
                        "bytes_remaining": 0,
                        "progress": 100,
                    }
                )
            elif not self.stop_requested:
                inventory["status"] = "incomplete"
            else:
                inventory["status"] = "paused"
            self.state.update(
                {
                    "status": "stopped" if self.stop_requested else "complete",
                    "phase": "Stopped safely" if self.stop_requested else "Library is up to date",
                    "progress": 100 if not self.stop_requested else self.state["progress"],
                    "finished_at": utc_now(),
                    "inventory": inventory,
                }
            )
        except Exception as exc:  # noqa: BLE001 - background jobs must always report terminal state
            inventory = dict(self.state["inventory"])
            inventory["status"] = "error"
            self.state.update(
                {
                    "status": "error",
                    "phase": "Sync failed",
                    "error": str(exc),
                    "finished_at": utc_now(),
                    "inventory": inventory,
                }
            )

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "source":
            self.state.update(
                {
                    "phase": payload.get("phase") or "Scanning group",
                    "current_source": payload.get("source"),
                }
            )
        elif event == "inventory_start":
            inventory = self._empty_inventory()
            sources = self.database.all(
                """SELECT sources.id,sources.telegram_id,sources.title,sources.last_message_id,
                (SELECT COUNT(1) FROM messages WHERE messages.source_id=sources.id
                    AND messages.status='complete') AS completed,
                (SELECT COUNT(1) FROM messages WHERE messages.source_id=sources.id
                    AND messages.status='failed') AS failed,
                (SELECT COUNT(1) FROM tracks JOIN messages ON messages.id=tracks.message_id
                    WHERE messages.source_id=sources.id) AS library_tracks
                FROM sources WHERE sources.enabled=1 ORDER BY sources.title COLLATE NOCASE"""
            )
            groups: dict[str, dict[str, Any]] = {}
            for source in sources:
                completed = int(source["completed"] or 0)
                groups[str(source["id"])] = {
                    "source_id": int(source["id"]),
                    "telegram_id": int(source["telegram_id"]),
                    "title": source["title"],
                    "status": "waiting",
                    "exact": False,
                    "previously_completed": completed,
                    "files_total": completed,
                    "files_processed": completed,
                    "files_remaining": 0,
                    "bytes_total": 0,
                    "bytes_processed": 0,
                    "bytes_remaining": 0,
                    "session_bytes_processed": 0,
                    "session_files_processed": 0,
                    "session_tracks_imported": 0,
                    "library_tracks": int(source["library_tracks"] or 0),
                    "failed": int(source["failed"] or 0),
                    "current_file": None,
                    "file_downloaded": 0,
                    "file_total": 0,
                    "file_progress": 0,
                    "last_message_id": int(source["last_message_id"] or 0),
                }
            previously_completed = sum(
                int(group["previously_completed"]) for group in groups.values()
            )
            inventory.update(
                {
                    "status": "scanning",
                    "previously_completed": previously_completed,
                    "files_total": previously_completed,
                    "files_processed": previously_completed,
                    "groups_total": int(payload.get("groups_total") or 0),
                    "groups": groups,
                }
            )
            self.state.update({"phase": "Building media inventory", "inventory": inventory})
        elif event == "inventory_source":
            inventory = dict(self.state["inventory"])
            groups, source_id, group = self._group_for_event(inventory, payload)
            group["status"] = "scanning"
            groups[source_id] = group
            inventory["groups"] = groups
            inventory["current_scan_source"] = payload.get("source")
            update: dict[str, Any] = {"inventory": inventory}
            if not self.state.get("current_file"):
                update.update(
                    {"phase": "Scanning group", "current_source": payload.get("source")}
                )
            self.state.update(update)
        elif event == "inventory_discovered":
            inventory = dict(self.state["inventory"])
            size = int(payload.get("size") or 0)
            inventory["files_total"] += 1
            inventory["files_remaining"] += 1
            inventory["bytes_total"] += size
            inventory["bytes_remaining"] += size
            groups, source_id, group = self._group_for_event(inventory, payload)
            group["files_total"] += 1
            group["files_remaining"] += 1
            group["bytes_total"] += size
            group["bytes_remaining"] += size
            groups[source_id] = group
            inventory["groups"] = groups
            self.state["inventory"] = inventory
            if payload.get("is_archive") and not payload.get("existing"):
                archive = dict(self.state["archive"])
                archive["total"] += 1
                archive["pending"] = max(0, archive["total"] - archive["processed"])
                self.state["archive"] = archive
        elif event == "inventory_group":
            inventory = dict(self.state["inventory"])
            inventory["groups_scanned"] += 1
            groups, source_id, group = self._group_for_event(inventory, payload)
            group["exact"] = True
            if group["status"] != "downloading":
                group["status"] = "queued" if group["files_remaining"] else "complete"
            groups[source_id] = group
            inventory["groups"] = groups
            self.state["inventory"] = inventory
        elif event == "inventory_ready":
            inventory = dict(self.state["inventory"])
            inventory.update({"status": "downloading", "exact": True})
            groups = dict(inventory["groups"])
            for source_id, current in groups.items():
                group = dict(current)
                group["exact"] = True
                if group["status"] in {"waiting", "scanning"}:
                    group["status"] = (
                        "queued" if group["files_remaining"] else "complete"
                    )
                groups[source_id] = group
            inventory["groups"] = groups
            update = {"inventory": inventory}
            if not self.state.get("current_file"):
                update["phase"] = "Media inventory ready"
            self.state.update(update)
            archive = dict(self.state["archive"])
            archive["exact"] = True
            self.state["archive"] = archive
        elif event == "download":
            downloaded = int(payload.get("downloaded") or 0)
            total = int(payload.get("total") or 0)
            inventory = dict(self.state["inventory"])
            if not inventory["exact"]:
                inventory["status"] = "scanning_downloading"
            inventory["bytes_processed"] = min(
                inventory["bytes_total"], self._inventory_completed_bytes + downloaded
            )
            inventory["bytes_remaining"] = max(
                0, inventory["bytes_total"] - inventory["bytes_processed"]
            )
            inventory["progress"] = round(
                inventory["bytes_processed"] / inventory["bytes_total"] * 100, 1
            ) if inventory["bytes_total"] else 0
            groups, source_id, group = self._group_for_event(inventory, payload)
            group.update(
                {
                    "status": "downloading",
                    "current_file": payload.get("filename"),
                    "file_downloaded": downloaded,
                    "file_total": total,
                    "file_progress": round(downloaded / total * 100, 1) if total else 0,
                }
            )
            group["bytes_processed"] = min(
                group["bytes_total"], group["session_bytes_processed"] + downloaded
            )
            group["bytes_remaining"] = max(
                0, group["bytes_total"] - group["bytes_processed"]
            )
            groups[source_id] = group
            inventory["groups"] = groups
            self.state.update(
                {
                    "phase": "Downloading media",
                    "current_file": payload.get("filename"),
                    "current_source": payload.get("source") or self.state["current_source"],
                    "downloaded": downloaded,
                    "total": total,
                    "progress": round(downloaded / total * 100, 1) if total else 0,
                    "error": None,
                    "inventory": inventory,
                }
            )
            archive = dict(self.state["archive"])
            current = dict(archive.get("current") or {})
            if str(payload.get("filename") or "").lower().endswith(".zip"):
                current.update(
                    {
                        "filename": payload.get("filename"),
                        "status": "downloading",
                        "compressed_size": total,
                        "bytes_downloaded": downloaded,
                        "progress": round(downloaded / total * 100, 1) if total else 0,
                    }
                )
                archive["current"] = current
                self.state["archive"] = archive
        elif event.startswith("archive_"):
            self._update_archive_state(event, payload)
        elif event == "processing":
            inventory = dict(self.state["inventory"])
            groups, source_id, group = self._group_for_event(inventory, payload)
            group.update({"status": "processing", "file_progress": 100})
            groups[source_id] = group
            inventory["groups"] = groups
            self.state.update(
                {"phase": "Reading metadata", "progress": 100, "inventory": inventory}
            )
        elif event == "imported":
            inventory = dict(self.state["inventory"])
            groups, source_id, group = self._group_for_event(inventory, payload)
            imported = int(payload.get("tracks") or 0)
            failure_count = self.database.one(
                "SELECT COUNT(*) AS count FROM messages WHERE source_id=? AND status='failed'",
                (int(payload.get("source_id") or 0),),
            )
            group["failed"] = int((failure_count or {}).get("count") or 0)
            group["session_tracks_imported"] += imported
            group["library_tracks"] += imported
            groups[source_id] = group
            inventory["groups"] = groups
            self.state["imported"] += imported
            self.state.update(
                {
                    "phase": "Track added to library",
                    "current_file": None,
                    "inventory": inventory,
                }
            )
        elif event == "inventory_processed":
            inventory = dict(self.state["inventory"])
            size = int(payload.get("size") or 0)
            self._inventory_completed_bytes += size
            inventory["files_processed"] += 1
            inventory["files_remaining"] = max(
                0, inventory["files_total"] - inventory["files_processed"]
            )
            inventory["bytes_processed"] = min(
                inventory["bytes_total"], self._inventory_completed_bytes
            )
            inventory["bytes_remaining"] = max(
                0, inventory["bytes_total"] - inventory["bytes_processed"]
            )
            inventory["progress"] = round(
                inventory["bytes_processed"] / inventory["bytes_total"] * 100, 1
            ) if inventory["bytes_total"] else 100
            groups, source_id, group = self._group_for_event(inventory, payload)
            group["files_processed"] += 1
            group["session_files_processed"] += 1
            group["files_remaining"] = max(
                0, group["files_total"] - group["files_processed"]
            )
            group["session_bytes_processed"] += size
            group["bytes_processed"] = min(
                group["bytes_total"], group["session_bytes_processed"]
            )
            group["bytes_remaining"] = max(
                0, group["bytes_total"] - group["bytes_processed"]
            )
            group.update(
                {
                    "status": "complete"
                    if group["exact"] and group["files_remaining"] == 0
                    else "queued",
                    "current_file": None,
                    "file_downloaded": 0,
                    "file_total": 0,
                    "file_progress": 0,
                }
            )
            groups[source_id] = group
            inventory["groups"] = groups
            self.state["inventory"] = inventory
        elif event == "failed":
            inventory = dict(self.state["inventory"])
            inventory["bytes_processed"] = min(
                inventory["bytes_total"], self._inventory_completed_bytes
            )
            inventory["bytes_remaining"] = max(
                0, inventory["bytes_total"] - inventory["bytes_processed"]
            )
            groups, source_id, group = self._group_for_event(inventory, payload)
            failure_count = self.database.one(
                "SELECT COUNT(*) AS count FROM messages WHERE source_id=? AND status='failed'",
                (int(payload.get("source_id") or 0),),
            )
            group["failed"] = int((failure_count or {}).get("count") or 0)
            group.update(
                {
                    "status": "error",
                    "current_file": payload.get("filename"),
                    "bytes_processed": min(
                        group["bytes_total"], group["session_bytes_processed"]
                    ),
                }
            )
            group["bytes_remaining"] = max(
                0, group["bytes_total"] - group["bytes_processed"]
            )
            groups[source_id] = group
            inventory["groups"] = groups
            self.state["failed"] += 1
            self.state.update(
                {"phase": "File failed", "error": payload.get("error"), "inventory": inventory}
            )
