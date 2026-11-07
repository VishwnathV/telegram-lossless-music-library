from fastapi.testclient import TestClient

import music_library.api as api_module
from music_library.api import app
from music_library.config import Settings
from music_library.dashboard import SyncManager, TelegramDashboard
from music_library.database import Database


def test_dashboard_config_is_stored_without_returning_hash(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
        tg_session_path=tmp_path / "telegram",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    dashboard = TelegramDashboard(settings, database)

    dashboard.save_config(123456, "0123456789abcdef0123456789abcdef")

    assert dashboard.credentials() == (123456, "0123456789abcdef0123456789abcdef")
    assert dashboard.config() == {
        "api_id": "123456",
        "api_hash_configured": True,
        "session_path": str(tmp_path / "telegram"),
    }


def test_dashboard_page_and_api_are_available(monkeypatch):
    async def disconnected_status():
        return {"configured": False, "connected": False, "account": None}

    monkeypatch.setattr("music_library.api.telegram.status", disconnected_status)
    client = TestClient(app)

    home = client.get("/")
    dashboard = client.get("/api/dashboard")

    assert home.status_code == 200
    assert "Resonance" in home.text
    assert "Your Telegram phone number" in home.text
    assert "Verification code" in home.text
    assert "Two-step verification" in home.text
    assert "speedNeedle" in home.text
    assert "TELEGRAM SPEED" in home.text
    assert "CURRENT FILE PROGRESS" in home.text
    assert "commandCluster" in home.text
    assert "LIBRARY CAPACITY" in home.text
    assert "MEDIA QUEUE" not in home.text
    assert "Telegram group analytics" in home.text
    assert dashboard.status_code == 200
    assert dashboard.json()["sync"]["status"] == "idle"
    assert dashboard.json()["sync"]["inventory"]["files_remaining"] == 0


def test_telegram_status_uses_cached_connection_during_sync(monkeypatch):
    cached = {
        "configured": True,
        "connected": True,
        "account": {"name": "Test Listener"},
    }

    monkeypatch.setattr(api_module.sync_manager, "snapshot", lambda: {"status": "running"})
    monkeypatch.setattr(api_module.telegram, "cached_status", lambda: cached)

    async def unexpected_live_status():
        raise AssertionError("an active sync must not start a second Telegram client")

    monkeypatch.setattr(api_module.telegram, "status", unexpected_live_status)

    response = TestClient(app).get("/api/telegram/status")

    assert response.status_code == 200
    assert response.json() == cached


def test_sync_inventory_tracks_global_remaining_work(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
        tg_session_path=tmp_path / "telegram",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as connection:
        source_id = connection.execute(
            "INSERT INTO sources(telegram_id,title,enabled) VALUES (?,?,1)",
            (123, "Existing source"),
        ).lastrowid
        connection.execute(
            """INSERT INTO messages(source_id,telegram_message_id,status)
            VALUES (?,?,'complete')""",
            (source_id, 456),
        )
    manager = SyncManager(settings, database, TelegramDashboard(settings, database))

    manager._event("inventory_start", {"groups_total": 2})
    manager._event("inventory_source", {"source_id": source_id, "source": "Existing source"})
    manager._event("inventory_discovered", {"source_id": source_id, "size": 100})
    manager._event("inventory_discovered", {"source_id": source_id, "size": 300})
    manager._event("inventory_group", {"source_id": source_id})
    manager._event("inventory_ready", {})
    manager._event(
        "download",
        {
            "source_id": source_id,
            "source": "Existing source",
            "filename": "album.zip",
            "downloaded": 40,
            "total": 100,
        },
    )

    inventory = manager.snapshot()["inventory"]
    groups = inventory.pop("groups")
    assert inventory == {
        "status": "downloading",
        "exact": True,
        "previously_completed": 1,
        "files_total": 3,
        "files_processed": 1,
        "files_remaining": 2,
        "bytes_total": 400,
        "bytes_processed": 40,
        "bytes_remaining": 360,
        "groups_total": 2,
        "groups_scanned": 1,
        "progress": 10.0,
        "current_scan_source": "Existing source",
    }
    assert len(groups) == 1
    assert groups[0]["title"] == "Existing source"
    assert groups[0]["status"] == "downloading"
    assert groups[0]["files_processed"] == 1
    assert groups[0]["files_remaining"] == 2
    assert groups[0]["file_progress"] == 40.0

    manager._event("inventory_processed", {"source_id": source_id, "size": 100})
    inventory = manager.snapshot()["inventory"]
    assert inventory["files_processed"] == 2
    assert inventory["files_remaining"] == 1
    assert inventory["bytes_processed"] == 100
    assert inventory["bytes_remaining"] == 300
    assert manager.snapshot()["progress"] == 40.0


def test_track_file_streams_inline_with_ranges(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    track_path = library / "sample.flac"
    track_path.write_bytes(b"fLaC" + bytes(4096))
    monkeypatch.setattr(api_module.settings, "library_dir", library)
    monkeypatch.setattr(
        api_module.database,
        "one",
        lambda *_args, **_kwargs: {
            "path": str(track_path),
            "filename": "sample.flac",
            "extension": ".flac",
        },
    )
    client = TestClient(app)

    response = client.get("/api/tracks/1/file", headers={"Range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == b"fLaC"
    assert response.headers["content-type"] == "audio/flac"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["accept-ranges"] == "bytes"
