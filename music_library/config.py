from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session_path: Path = Path("./data/telegram")
    tg_sources: str = ""

    data_dir: Path = Path("./data")
    incoming_dir: Path = Path("./incoming")
    library_dir: Path = Path("./library")
    database_path: Path = Path("./data/library.sqlite3")
    playback_cache_dir: Path = Path("./data/playback-cache")

    max_download_bytes: int = 10 * 1024**3
    max_archive_files: int = 5000
    max_archive_bytes: int = 20 * 1024**3
    telegram_download_part_kb: int = 512
    keep_incoming: bool = False

    @property
    def sources(self) -> list[str | int]:
        parsed: list[str | int] = []
        for raw in self.tg_sources.split(","):
            value = raw.strip()
            if not value:
                continue
            try:
                parsed.append(int(value))
            except ValueError:
                parsed.append(value)
        return parsed

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.incoming_dir,
            self.library_dir,
            self.playback_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.tg_session_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
