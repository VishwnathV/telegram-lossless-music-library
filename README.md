# Resonance - Telegram Smart Music Library

Resonance turns music shared in Telegram groups you are authorized to access into a searchable,
lossless-first private library. It downloads audio and ZIP attachments, safely extracts audio,
reads embedded tags, de-duplicates by SHA-256, organizes files by artist and album, and provides
a responsive operations dashboard and player.

> Use this only for groups and media you have permission to access and store. Resonance does not
> bypass Telegram access controls or DRM.

## Architecture

```text
Authorized Telegram groups
          |
  Telethon user session
          |
 Type, size and archive checks
          |
 Metadata + SHA-256 dedupe
          |
  +-------+----------------+
  |                        |
Artist/Album files    SQLite catalog
                           |
                  FastAPI dashboard/API
```

The collector uses a Telegram **user session**, because a Bot API bot cannot freely backfill
arbitrary group history. The session file is a login credential: keep it private, never commit
it, and consider using a dedicated Telegram account.

## Quick start

1. Install Python 3.11 or newer:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   music-library init
   music-library serve --host 127.0.0.1 --port 8000
   ```

2. Open `http://127.0.0.1:8000`.

3. Select **Telegram Sources** in the dashboard:

   - Create an API application at `https://my.telegram.org`.
   - Save its API ID and API hash in the local dashboard.
   - Enter your phone number, Telegram login code, and 2FA password when applicable.
   - Refresh the group list, choose approved sources, and save the selection.
   - Return to Overview and select **Start sync**.

Credentials and the Telegram session stay on this computer. The API ID and hash can alternatively
be provided through `.env`; see `.env.example`.

You can import an existing local collection with:

```powershell
music-library scan D:\Music
```

## Dashboard

- Live Telegram connection and account status
- Group and channel discovery with per-source selection
- Start and safe-stop synchronization controls
- Current source, filename, transfer progress, and error reporting
- Tracks, albums, artists, lossless ratio, formats, duration, and disk statistics
- Searchable track table, album grid, quality metadata, and built-in player
- Completed and failed import activity
- Responsive desktop, tablet, and mobile layouts

## Media pipeline

- MP3, FLAC, WAV, ALAC/M4A, AAC, AIFF, APE, DSF, OGG, Opus, and WMA candidates
- ZIP path-traversal, file-count, and expansion-size protection
- Restartable sync using the last successfully processed Telegram message ID
- Failed-message recording and retry on the next sync
- Exact content de-duplication even when filenames differ
- Tag-based `Artist/[Year - ]Album/Track - Title.ext` organization
- Original lossless files preserved without conversion

Some formats depend on what Mutagen can parse. Untagged files fall back to the filename and
`Unknown Artist/Unknown Album`.

## Security boundary

The dashboard intentionally binds to `127.0.0.1` by default and does not include web-user
authentication. Do not expose it directly to the public internet. Add HTTPS, authentication,
authorization, and per-user media permissions before any remote or multi-user deployment.

## Roadmap

Useful next additions include cover-art extraction, MusicBrainz/AcoustID enrichment with explicit
privacy controls, acoustic-fingerprint duplicate detection, playlists and favorites, preview
transcoding, PostgreSQL/worker queues for larger installations, and an optional official Telegram
command bot that searches and returns permitted library files.
