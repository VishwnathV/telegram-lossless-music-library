import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from music_library.config import Settings
from music_library.database import Database
from music_library.telegram_collector import TelegramCollector


def test_download_retries_with_smaller_parts_and_completes(tmp_path, monkeypatch):
    settings = Settings(
        tg_api_id=123,
        tg_api_hash="0123456789abcdef0123456789abcdef",
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
        tg_session_path=tmp_path / "telegram",
        telegram_download_part_kb=512,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as connection:
        source_id = connection.execute(
            "INSERT INTO sources(telegram_id,title) VALUES (?,?)", (123, "Test source")
        ).lastrowid

    collector = TelegramCollector(settings, database)
    monkeypatch.setattr(collector.cataloger, "expand", lambda *_args: [])
    part_sizes = []

    class FakeClient:
        async def download_file(self, _location, file, **kwargs):
            part_sizes.append(kwargs["part_size_kb"])
            if len(part_sizes) == 1:
                raise ValueError("temporary Telegram transport failure")
            Path(file).write_bytes(b"downloaded")
            kwargs["progress_callback"](10, 10)

    collector.client = FakeClient()
    message = SimpleNamespace(
        id=77,
        date=datetime.now(UTC),
        file=SimpleNamespace(name="song.mp3", size=10),
        document=SimpleNamespace(id=1, access_hash=2, file_reference=b"ref", dc_id=4),
    )

    succeeded = asyncio.run(collector._process_message(source_id, message, "Test source"))

    assert succeeded is True
    assert part_sizes == [512, 128]
    assert database.one(
        "SELECT status,error FROM messages WHERE source_id=? AND telegram_message_id=?",
        (source_id, 77),
    ) == {"status": "complete", "error": None}
