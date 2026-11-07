from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    username TEXT,
    kind TEXT NOT NULL DEFAULT 'group',
    enabled INTEGER NOT NULL DEFAULT 1,
    member_count INTEGER,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    telegram_date TEXT,
    original_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    UNIQUE(source_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_seconds REAL,
    bitrate INTEGER,
    sample_rate INTEGER,
    bit_depth INTEGER,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    album_artist TEXT,
    genre TEXT,
    year INTEGER,
    track_number INTEGER,
    disc_number INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS favourites (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album);
CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_favourites_created ON favourites(created_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # Small forward-only migrations keep existing MVP databases usable.
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(sources)").fetchall()
            }
            source_columns = {
                "username": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'group'",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "member_count": "INTEGER",
            }
            for name, definition in source_columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE sources ADD COLUMN {name} {definition}")
            try:
                connection.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
                    title, artist, album, genre, content='tracks', content_rowid='id'
                    )"""
                )
                connection.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
                      INSERT INTO tracks_fts(rowid,title,artist,album,genre)
                      VALUES (new.id,new.title,new.artist,new.album,new.genre);
                    END;
                    CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
                      INSERT INTO tracks_fts(tracks_fts,rowid,title,artist,album,genre)
                      VALUES('delete',old.id,old.title,old.artist,old.album,old.genre);
                    END;
                    CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
                      INSERT INTO tracks_fts(tracks_fts,rowid,title,artist,album,genre)
                      VALUES('delete',old.id,old.title,old.artist,old.album,old.genre);
                      INSERT INTO tracks_fts(rowid,title,artist,album,genre)
                      VALUES (new.id,new.title,new.artist,new.album,new.genre);
                    END;
                    """
                )
            except sqlite3.OperationalError:
                # Some minimal SQLite builds omit FTS5. LIKE search remains available.
                pass

    def one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM app_settings WHERE key=?", (key,))
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key=?", (key,))
