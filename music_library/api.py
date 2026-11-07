from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

import imageio_ffmpeg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import get_settings
from .dashboard import SyncManager, TelegramDashboard
from .database import Database

settings = get_settings()
database = Database(settings.database_path)
database.initialize()
telegram = TelegramDashboard(settings, database)
sync_manager = SyncManager(settings, database, telegram)
app = FastAPI(title="Resonance Music Library", version="0.2.0")
static_dir = Path(__file__).with_name("static")
AUDIO_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".alac": "audio/mp4",
    ".ape": "audio/x-ape",
    ".dsf": "audio/dsf",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
}
NATIVE_PLAYBACK_EXTENSIONS = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
PLAYBACK_TRANSCODE_LOCK = threading.Lock()


def _track_record(track_id: int) -> tuple[dict, Path]:
    track = database.one("SELECT path,filename,extension FROM tracks WHERE id=?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")
    path = Path(track["path"])
    try:
        path.resolve().relative_to(settings.library_dir.resolve())
    except ValueError as exc:
        raise HTTPException(403, "Invalid library path") from exc
    if not path.is_file():
        raise HTTPException(410, "Track file is missing")
    return track, path


def _browser_playback_file(track_id: int, path: Path) -> Path:
    fingerprint = f"{track_id}-{path.stat().st_size}-{path.stat().st_mtime_ns}"
    target = settings.playback_cache_dir / f"{fingerprint}.mp3"
    if target.is_file() and target.stat().st_size:
        return target
    settings.playback_cache_dir.mkdir(parents=True, exist_ok=True)
    with PLAYBACK_TRANSCODE_LOCK:
        if target.is_file() and target.stat().st_size:
            return target
        temporary = target.with_suffix(".temporary.mp3")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map_metadata",
            "-1",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-threads",
            "1",
            "-y",
            str(temporary),
        ]
        try:
            subprocess.run(command, check=True, timeout=300, capture_output=True)
            os.replace(temporary, target)
        except (OSError, subprocess.SubprocessError) as exc:
            temporary.unlink(missing_ok=True)
            detail = getattr(exc, "stderr", b"")
            message = detail.decode("utf-8", "replace").strip() if detail else str(exc)
            raise HTTPException(500, f"Could not prepare browser audio: {message[-300:]}") from exc
    return target


class TelegramConfigInput(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=20, max_length=128)


class PhoneInput(BaseModel):
    phone: str = Field(min_length=7, max_length=32)


class VerifyInput(BaseModel):
    code: str | None = Field(default=None, min_length=2, max_length=16)
    password: str | None = Field(default=None, max_length=256)


class SourceSelection(BaseModel):
    telegram_ids: list[int]


class FavouriteInput(BaseModel):
    favourite: bool = True


def bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def library_stats() -> dict:
    totals = database.one(
        """SELECT COUNT(*) AS tracks,COUNT(DISTINCT artist) AS artists,
        COUNT(DISTINCT artist || char(0) || album) AS albums,
        COALESCE(SUM(size_bytes),0) AS size_bytes,COALESCE(SUM(duration_seconds),0) AS seconds,
        COALESCE(SUM(CASE WHEN extension IN ('.flac','.wav','.aif','.aiff','.alac','.ape','.dsf')
            THEN 1 ELSE 0 END),0) AS lossless
        FROM tracks"""
    ) or {}
    messages = database.one(
        """SELECT COUNT(*) AS files,
        COALESCE(SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END),0) AS complete,
        COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
        FROM messages"""
    ) or {}
    source_counts = database.one(
        """SELECT COUNT(*) AS available,
        COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),0) AS enabled FROM sources"""
    ) or {}
    usage = shutil.disk_usage(settings.library_dir)
    return {
        **totals,
        "messages": messages,
        "sources": source_counts,
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
    }


@app.get("/api/dashboard")
async def dashboard():
    sync = sync_manager.snapshot()
    telegram_state = (
        telegram.cached_status()
        if sync["status"] in {"running", "stopping"}
        else await telegram.status()
    )
    return {
        "stats": library_stats(),
        "telegram": telegram_state,
        "sync": sync,
        "formats": database.all(
            """SELECT UPPER(LTRIM(extension,'.')) AS format,COUNT(*) AS count,
            COALESCE(SUM(size_bytes),0) AS size_bytes FROM tracks
            GROUP BY extension ORDER BY count DESC"""
        ),
        "recent": database.all("SELECT * FROM tracks ORDER BY created_at DESC LIMIT 8"),
    }


@app.get("/api/events")
async def events():
    async def stream():
        previous = ""
        while True:
            payload = json.dumps(sync_manager.snapshot(), separators=(",", ":"))
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/telegram/config")
def telegram_config():
    return telegram.config()


@app.post("/api/telegram/config")
def save_telegram_config(payload: TelegramConfigInput):
    telegram.save_config(payload.api_id, payload.api_hash)
    return telegram.config()


@app.get("/api/telegram/status")
async def telegram_status():
    return await telegram.status()


@app.post("/api/telegram/login/code")
async def telegram_login_code(payload: PhoneInput):
    try:
        return await telegram.send_code(payload.phone.strip())
    except Exception as exc:
        raise bad_request(exc) from exc


@app.post("/api/telegram/login/verify")
async def telegram_login_verify(payload: VerifyInput):
    if not payload.code and not payload.password:
        raise HTTPException(400, "Enter the verification code or 2FA password")
    try:
        return await telegram.verify(payload.code, payload.password)
    except Exception as exc:
        raise bad_request(exc) from exc


@app.post("/api/telegram/logout")
async def telegram_logout():
    if sync_manager.snapshot()["status"] in {"running", "stopping"}:
        raise HTTPException(409, "Stop synchronization before disconnecting Telegram")
    try:
        await telegram.logout()
        return {"connected": False}
    except Exception as exc:
        raise bad_request(exc) from exc


@app.get("/api/sources")
def sources():
    return database.all(
        """SELECT id,telegram_id,title,username,kind,enabled,member_count,last_message_id,updated_at
        FROM sources ORDER BY enabled DESC,title COLLATE NOCASE"""
    )


@app.post("/api/sources/discover")
async def discover_sources():
    if sync_manager.snapshot()["status"] in {"running", "stopping"}:
        raise HTTPException(409, "Stop synchronization before refreshing Telegram groups")
    try:
        await telegram.discover_groups()
        return sources()
    except Exception as exc:
        raise bad_request(exc) from exc


@app.put("/api/sources/selection")
def select_sources(payload: SourceSelection):
    with database.connect() as connection:
        connection.execute("UPDATE sources SET enabled=0")
        if payload.telegram_ids:
            placeholders = ",".join("?" for _ in payload.telegram_ids)
            connection.execute(
                f"UPDATE sources SET enabled=1 WHERE telegram_id IN ({placeholders})",
                tuple(payload.telegram_ids),
            )
    return sources()


@app.post("/api/sync/start")
async def start_sync():
    try:
        return await sync_manager.start()
    except Exception as exc:
        raise bad_request(exc) from exc


@app.post("/api/sync/stop")
def stop_sync():
    return sync_manager.stop()


@app.get("/api/sync/status")
def sync_status():
    return sync_manager.snapshot()


@app.get("/api/activity")
def activity(limit: int = Query(50, ge=1, le=250)):
    return database.all(
        """SELECT messages.id,messages.original_name,messages.status,messages.error,
        messages.telegram_date,sources.title AS source,
        COUNT(tracks.id) AS tracks_imported,COALESCE(SUM(tracks.size_bytes),0) AS size_bytes
        FROM messages JOIN sources ON sources.id=messages.source_id
        LEFT JOIN tracks ON tracks.message_id=messages.id
        GROUP BY messages.id ORDER BY messages.id DESC LIMIT ?""",
        (limit,),
    )


@app.get("/api/tracks")
def tracks(
    q: str = "",
    format: str = "",
    quality: str = "",
    artist: str = "",
    album: str = "",
    genre: str = "",
    year: int | None = None,
    favourite: bool = False,
    sort: str = "recent",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    clauses: list[str] = []
    parameters: list[object] = []
    if q.strip():
        needle = f"%{q.strip()}%"
        clauses.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR genre LIKE ?)")
        parameters.extend((needle, needle, needle, needle))
    if format.strip():
        clauses.append("extension=?")
        parameters.append("." + format.strip().lower().lstrip("."))
    if quality == "lossless":
        clauses.append(
            "extension IN ('.flac','.wav','.aif','.aiff','.alac','.ape','.dsf')"
        )
    elif quality == "hires":
        clauses.append("(COALESCE(sample_rate,0)>=88200 OR COALESCE(bit_depth,0)>=24)")
    if artist.strip():
        clauses.append("artist=?")
        parameters.append(artist.strip())
    if album.strip():
        clauses.append("album=?")
        parameters.append(album.strip())
    if genre.strip():
        clauses.append("genre=?")
        parameters.append(genre.strip())
    if year is not None:
        clauses.append("year=?")
        parameters.append(year)
    if favourite:
        clauses.append("EXISTS (SELECT 1 FROM favourites WHERE track_id=tracks.id)")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order_by = {
        "recent": "created_at DESC",
        "title": "title COLLATE NOCASE,artist COLLATE NOCASE",
        "artist": "artist COLLATE NOCASE,album COLLATE NOCASE,track_number",
        "album": "album COLLATE NOCASE,disc_number,track_number,title COLLATE NOCASE",
        "year": "year DESC,album COLLATE NOCASE,track_number",
        "quality": "COALESCE(bit_depth,0) DESC,COALESCE(sample_rate,0) DESC,bitrate DESC",
        "size": "size_bytes DESC",
    }.get(sort, "created_at DESC")
    parameters.extend((limit, offset))
    return database.all(
        f"""SELECT tracks.*,
        EXISTS (SELECT 1 FROM favourites WHERE track_id=tracks.id) AS favourite
        FROM tracks{where} ORDER BY {order_by} LIMIT ? OFFSET ?""",
        tuple(parameters),
    )


@app.get("/api/albums")
def albums(q: str = "", limit: int = Query(250, ge=1, le=1000)):
    where = "WHERE album LIKE ? OR artist LIKE ?" if q.strip() else ""
    parameters: tuple[object, ...] = (
        (f"%{q.strip()}%", f"%{q.strip()}%", limit) if q.strip() else (limit,)
    )
    return database.all(
        f"""SELECT artist,album,year,COUNT(*) AS track_count,SUM(size_bytes) AS size_bytes,
        MAX(extension) AS format,MAX(bit_depth) AS bit_depth,MAX(sample_rate) AS sample_rate,
        MIN(id) AS first_track_id,MAX(created_at) AS added_at FROM tracks {where}
        GROUP BY artist,album,year ORDER BY added_at DESC,album COLLATE NOCASE LIMIT ?""",
        parameters,
    )


@app.get("/api/artists")
def artists(q: str = "", limit: int = Query(250, ge=1, le=1000)):
    where = "WHERE artist LIKE ?" if q.strip() else ""
    parameters: tuple[object, ...] = (f"%{q.strip()}%", limit) if q.strip() else (limit,)
    return database.all(
        f"""SELECT artist,COUNT(*) AS track_count,COUNT(DISTINCT album) AS album_count,
        COALESCE(SUM(duration_seconds),0) AS seconds,MAX(created_at) AS added_at,
        MIN(id) AS first_track_id FROM tracks {where} GROUP BY artist
        ORDER BY track_count DESC,artist COLLATE NOCASE LIMIT ?""",
        parameters,
    )


@app.get("/api/genres")
def genres(q: str = "", limit: int = Query(100, ge=1, le=500)):
    where = "WHERE genre IS NOT NULL AND TRIM(genre)<>''"
    parameters: list[object] = []
    if q.strip():
        where += " AND genre LIKE ?"
        parameters.append(f"%{q.strip()}%")
    parameters.append(limit)
    return database.all(
        f"""SELECT genre,COUNT(*) AS track_count,COUNT(DISTINCT album) AS album_count,
        COALESCE(SUM(duration_seconds),0) AS seconds,MIN(id) AS first_track_id
        FROM tracks {where} GROUP BY genre ORDER BY track_count DESC,genre COLLATE NOCASE LIMIT ?""",
        tuple(parameters),
    )


@app.get("/api/library/summary")
def library_summary():
    return database.one(
        """SELECT COUNT(*) AS tracks,COUNT(DISTINCT artist) AS artists,
        COUNT(DISTINCT album) AS albums,COUNT(DISTINCT NULLIF(TRIM(genre),'')) AS genres,
        COALESCE(SUM(duration_seconds),0) AS seconds,
        COALESCE(SUM(size_bytes),0) AS size_bytes,
        SUM(CASE WHEN extension IN ('.flac','.wav','.aif','.aiff','.alac','.ape','.dsf')
            THEN 1 ELSE 0 END) AS lossless,
        (SELECT COUNT(*) FROM favourites) AS favourites FROM tracks"""
    )


@app.put("/api/tracks/{track_id}/favourite")
def set_favourite(track_id: int, payload: FavouriteInput):
    if not database.one("SELECT id FROM tracks WHERE id=?", (track_id,)):
        raise HTTPException(404, "Track not found")
    with database.connect() as connection:
        if payload.favourite:
            connection.execute(
                "INSERT OR IGNORE INTO favourites(track_id) VALUES (?)", (track_id,)
            )
        else:
            connection.execute("DELETE FROM favourites WHERE track_id=?", (track_id,))
    return {"track_id": track_id, "favourite": payload.favourite}


@app.get("/api/stats")
def stats():
    return library_stats()


@app.get("/api/tracks/{track_id}/file")
def track_file(track_id: int):
    track, path = _track_record(track_id)
    media_type = AUDIO_MEDIA_TYPES.get(track["extension"].lower(), "application/octet-stream")
    disposition = f"inline; filename*=utf-8''{quote(track['filename'])}"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/tracks/{track_id}/play")
def play_track(track_id: int):
    track, path = _track_record(track_id)
    extension = track["extension"].lower()
    if extension in NATIVE_PLAYBACK_EXTENSIONS:
        media_type = AUDIO_MEDIA_TYPES[extension]
        return FileResponse(path, media_type=media_type)
    proxy = _browser_playback_file(track_id, path)
    return FileResponse(
        proxy,
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")
