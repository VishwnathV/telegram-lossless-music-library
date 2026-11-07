import zipfile
from pathlib import Path

import pytest

from music_library.catalog import Cataloger, TrackMetadata, safe_component, safe_filename
from music_library.config import Settings
from music_library.database import Database


@pytest.mark.parametrize(
    ("value", "expected"),
    [("AC/DC", "AC_DC"), ("  Album. ", "Album"), ("", "Unknown")],
)
def test_safe_component(value: str, expected: str):
    assert safe_component(value, "Unknown") == expected


def test_safe_component_limits_length():
    assert len(safe_component("x" * 200, "Unknown")) == 150


def test_safe_component_does_not_create_trailing_space_when_truncated():
    assert safe_component("Artist name followed by words", "Unknown", 12) == "Artist name"


def test_safe_filename_removes_windows_invalid_characters_and_preserves_extension():
    original = 'Sanju_Rathod,*Sonali_Sonawane_Love_You_Na_Yaar_From*"Love_You_Na.mp3'

    result = safe_filename(original, "telegram-241")

    assert result == "Sanju_Rathod,_Sonali_Sonawane_Love_You_Na_Yaar_From__Love_You_Na.mp3"
    assert not any(character in result for character in '<>:"/\\|?*')


@pytest.mark.parametrize("name", ["CON.mp3", "nul.flac", "LPT1.wav"])
def test_safe_filename_avoids_windows_reserved_names(name: str):
    assert safe_filename(name, "telegram").startswith("_")


def test_zip_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../outside.mp3", b"not actually audio")
    settings = Settings(
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
    )
    database = Database(settings.database_path)
    database.initialize()

    with pytest.raises(ValueError, match="unsafe archive path"):
        Cataloger(settings, database).expand(archive, tmp_path / "extract")

    assert not (tmp_path / "outside.mp3").exists()


def test_zip_windows_invalid_names_are_safely_extracted(tmp_path):
    archive = tmp_path / "windows-names.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr('Album/Bad*"Song?.mp3', b"audio bytes")
    settings = Settings(
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
    )
    database = Database(settings.database_path)
    database.initialize()

    extracted = Cataloger(settings, database).expand(archive, tmp_path / "extract")

    assert len(extracted) == 1
    assert extracted[0].is_file()
    assert extracted[0].name == "Bad__Song_.mp3"


def test_catalog_paths_are_bounded_but_original_metadata_is_preserved(tmp_path, monkeypatch):
    source = tmp_path / "source.flac"
    source.write_bytes(b"fake audio")
    metadata = TrackMetadata(
        title="Very Long Song Title " * 20,
        artist="Artist and Featured Performer " * 20,
        album="An Extremely Long Album Name " * 20,
        year=2026,
        track_number=1,
    )
    monkeypatch.setattr("music_library.catalog.read_metadata", lambda _path: metadata)
    settings = Settings(
        data_dir=tmp_path / "data",
        incoming_dir=tmp_path / "incoming",
        library_dir=tmp_path / "library",
        database_path=tmp_path / "catalog.sqlite3",
    )
    database = Database(settings.database_path)
    database.initialize()

    track_id, created = Cataloger(settings, database).ingest(source)
    track = database.one("SELECT path,title,artist,album FROM tracks WHERE id=?", (track_id,))

    assert created is True
    assert track is not None
    assert len(str(Path(track["path"]))) <= 240
    assert Path(track["path"]).is_file()
    assert track["title"] == metadata.title
    assert track["artist"] == metadata.artist
    assert track["album"] == metadata.album


def test_database_initializes_and_reports_empty_library(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    database.initialize()

    result = database.one("SELECT COUNT(*) AS count FROM tracks")

    assert result == {"count": 0}
