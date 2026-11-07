from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from .catalog import AUDIO_EXTENSIONS, Cataloger
from .config import get_settings
from .database import Database
from .telegram_collector import TelegramCollector


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Telegram smart music library")
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create directories and database")
    commands.add_parser("auth", help="create/verify the Telegram user session")
    commands.add_parser("sync", help="download new media from configured Telegram sources")
    scan = commands.add_parser("scan", help="import local audio files")
    scan.add_argument("path", type=Path)
    serve = commands.add_parser("serve", help="start the library web UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    return root


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init":
        print(f"Initialized database at {settings.database_path.resolve()}")
    elif args.command == "auth":
        asyncio.run(TelegramCollector(settings, database).authorize())
    elif args.command == "sync":
        asyncio.run(TelegramCollector(settings, database).sync())
    elif args.command == "scan":
        cataloger = Cataloger(settings, database)
        paths = [args.path] if args.path.is_file() else args.path.rglob("*")
        imported = 0
        for path in paths:
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                _, created = cataloger.ingest(path)
                imported += int(created)
        print(f"Imported {imported} new tracks")
    elif args.command == "serve":
        uvicorn.run("music_library.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()

