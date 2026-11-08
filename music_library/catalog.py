from __future__ import annotations

import hashlib
import logging
import re
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mutagen import File as MutagenFile

from .config import Settings
from .database import Database

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".alac", ".ape", ".dsf", ".flac", ".m4a", ".mp3",
    ".ogg", ".opus", ".wav", ".wma",
}
ARCHIVE_EXTENSIONS = {".zip"}
INVALID_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(slots=True)
class TrackMetadata:
    title: str
    artist: str
    album: str
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    duration_seconds: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None


def safe_component(value: str | None, fallback: str, max_length: int = 150) -> str:
    cleaned = INVALID_PATH.sub("_", (value or "").strip()).strip(" .")
    cleaned = cleaned[:max_length].rstrip(" .") or fallback[:max_length].rstrip(" .")
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"[:max_length]
    return cleaned


def safe_filename(
    value: str | None, fallback_stem: str, max_stem_length: int = 150
) -> str:
    """Return a single Windows-safe filename while preserving its extension."""
    basename = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(basename).suffix.lower()
    stem = basename[: -len(suffix)] if suffix else basename
    return safe_component(stem, fallback_stem, max_stem_length) + suffix


def safe_archive_target(destination: Path, member_name: str) -> Path:
    """Map an archive member to a bounded, traversal-safe local extraction path."""
    archive_path = PurePosixPath(member_name.replace("\\", "/"))
    if archive_path.is_absolute() or ".." in archive_path.parts:
        raise ValueError(f"unsafe archive path: {member_name}")
    parts = [part for part in archive_path.parts if part not in {"", "."}]
    if not parts:
        raise ValueError(f"unsafe archive path: {member_name}")
    directories = [safe_component(part, "folder", 36) for part in parts[:-1]][-2:]
    filename = safe_filename(parts[-1], "track", 96)
    root = destination.resolve()
    target = root.joinpath(*directories, filename).resolve()
    if root not in target.parents:
        raise ValueError(f"unsafe archive path: {member_name}")
    return target


def _first(tags: object, keys: Iterable[str]) -> str | None:
    if not tags:
        return None
    for key in keys:
        try:
            value = tags.get(key)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            continue
        if value:
            if isinstance(value, (list, tuple)):
                value = value[0]
            return str(value)
    return None


def _number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def read_metadata(path: Path) -> TrackMetadata:
    media = MutagenFile(path, easy=True)
    tags = getattr(media, "tags", None)
    info = getattr(media, "info", None)
    return TrackMetadata(
        title=_first(tags, ("title",)) or path.stem,
        artist=_first(tags, ("artist", "albumartist")) or "Unknown Artist",
        album=_first(tags, ("album",)) or "Unknown Album",
        album_artist=_first(tags, ("albumartist",)),
        genre=_first(tags, ("genre",)),
        year=_number(_first(tags, ("date", "year"))),
        track_number=_number(_first(tags, ("tracknumber",))),
        disc_number=_number(_first(tags, ("discnumber",))),
        duration_seconds=getattr(info, "length", None),
        bitrate=getattr(info, "bitrate", None),
        sample_rate=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Cataloger:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def expand(
        self,
        source: Path,
        destination: Path,
        on_progress: Callable[[str, dict[str, int | str]], None] | None = None,
    ) -> list[Path]:
        if source.suffix.lower() not in ARCHIVE_EXTENSIONS:
            return [source]
        destination.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        report = on_progress or (lambda _event, _payload: None)
        with zipfile.ZipFile(source) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > self.settings.max_archive_files:
                raise ValueError("archive contains too many files")
            total_size = 0
            audio_size = 0
            planned: list[tuple[zipfile.ZipInfo, Path]] = []
            for member in members:
                total_size += member.file_size
                if total_size > self.settings.max_archive_bytes:
                    raise ValueError("archive expands beyond the configured safety limit")
                target = safe_archive_target(destination, member.filename)
                if target.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                audio_size += member.file_size
                planned.append((member, target))
            report(
                "archive_validated",
                {
                    "entries_total": len(members),
                    "audio_entries": len(planned),
                    "ignored_entries": len(members) - len(planned),
                    "expanded_size": total_size,
                    "audio_size": audio_size,
                },
            )
            bytes_extracted = 0
            for index, (member, target) in enumerate(planned, start=1):
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    suffix = hashlib.sha256(member.filename.encode("utf-8")).hexdigest()[:8]
                    target = target.with_stem(f"{target.stem[:80]} [{suffix}]")
                with archive.open(member) as source_handle, target.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                extracted.append(target)
                bytes_extracted += member.file_size
                report(
                    "archive_extracting",
                    {
                        "entries_processed": index,
                        "audio_entries": len(planned),
                        "bytes_extracted": bytes_extracted,
                        "audio_size": audio_size,
                        "current_entry": member.filename,
                    },
                )
            report(
                "archive_extracted",
                {
                    "entries_processed": len(planned),
                    "audio_entries": len(planned),
                    "bytes_extracted": bytes_extracted,
                    "audio_size": audio_size,
                },
            )
        return extracted

    def ingest(self, path: Path, message_id: int | None = None) -> tuple[int, bool]:
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"unsupported audio format: {path.suffix}")
        digest = sha256_file(path)
        existing = self.database.one("SELECT id FROM tracks WHERE sha256 = ?", (digest,))
        if existing:
            return int(existing["id"]), False

        metadata = read_metadata(path)
        # Conservative component limits keep complete paths below legacy Windows MAX_PATH.
        # Original metadata remains intact in SQLite and the UI.
        artist = safe_component(
            metadata.album_artist or metadata.artist, "Unknown Artist", 48
        )
        album_label = metadata.album
        if metadata.year:
            album_label = f"{metadata.year} - {album_label}"
        album = safe_component(album_label, "Unknown Album", 56)
        prefix = f"{metadata.track_number:02d} - " if metadata.track_number is not None else ""
        filename = safe_component(prefix + metadata.title, path.stem, 72) + path.suffix.lower()
        target = self.settings.library_dir / artist / album / filename
        if len(str(target.resolve())) > 240:
            overflow = len(str(target.resolve())) - 240
            shorter_stem = target.stem[: max(24, len(target.stem) - overflow)]
            target = target.with_name(shorter_stem + target.suffix)
        if len(str(target.resolve())) > 240:
            raise ValueError("library directory path is too long for safe Windows storage")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            suffix_label = f" [{digest[:8]}]"
            available = max(16, 240 - len(str(target.parent.resolve())) - len(target.suffix) - 1)
            target = target.with_stem(
                f"{target.stem[: max(1, available - len(suffix_label))]}{suffix_label}"
            )
        shutil.copy2(path, target)

        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO tracks(
                    message_id,sha256,path,filename,extension,size_bytes,duration_seconds,
                    bitrate,sample_rate,bit_depth,title,artist,album,album_artist,genre,year,
                    track_number,disc_number
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    message_id, digest, str(target.resolve()), target.name, target.suffix.lower(),
                    target.stat().st_size, metadata.duration_seconds, metadata.bitrate,
                    metadata.sample_rate, metadata.bit_depth, metadata.title, metadata.artist,
                    metadata.album, metadata.album_artist, metadata.genre, metadata.year,
                    metadata.track_number, metadata.disc_number,
                ),
            )
            return int(cursor.lastrowid), True
