# phobos

Personal app for managing your photos and videos — local, private, no cloud.

- **Timeline** of everything, grouped by month, with a year/month jump rail
- **Viewer** with keyboard navigation, details panel, description, tags, date fixing
- **Favorites, albums, tags, search** (names, descriptions, tags, cameras, dates)
- **Albums sort your library**: items in an album leave the Photos/Videos tabs (a
  "Show items in albums" switch brings them back). One item can be in many albums and
  is still stored as a single file
- **Import** by drag & drop in the browser, or from a folder path (Windows paths work)
- **Duplicate detection** by content (SHA-256), whatever the file name — each import
  can list the files it skipped next to the library items they matched
- **Import history**: which folder was imported when, with counts (also in the server log)
- **Trash** with restore; nothing is deleted from disk until you empty it
- Supports JPEG, PNG, WebP, GIF, HEIC/HEIF, AVIF, TIFF, BMP and MP4, MOV, WebM, MKV, AVI, 3GP, MTS

## Quick start (Ubuntu on WSL)

```bash
python3 -m venv .venv && . .venv/bin/activate
make install
sudo apt install ffmpeg      # optional, for video thumbnails and video metadata
make serve                   # or: phobos-web
```

Open <http://localhost:8005> in Firefox/Chrome on Windows (WSL forwards `localhost`).

Import your first photos from the **Import** page:

- **Choose folder** opens Phobos' own folder browser (your Pictures/OneDrive folder and
  every Windows drive are one click away). The server imports the folder directly, so
  there's no browser "upload all files from this folder?" prompt and nothing is sent over HTTP.
- Or paste a path straight from Explorer's address bar (`C:\Users\you\Pictures`).
- Or drop files/folders onto the page (these are uploaded through the browser).

## Where your files live

| What                       | Default                        | Why                                                                                                           |
|----------------------------|--------------------------------|---------------------------------------------------------------------------------------------------------------|
| Originals (`library_dir`)  | `~/Phobos`                     | Plain folders `YYYY/MM/20240715_143012_ab12cd34.jpg`. Readable without Phobos — no lock-in.                    |
| DB + cache (`data_dir`)    | `~/.local/share/phobos`        | `phobos.db` (SQLite) and WebP thumbnails/previews. Must be on the Linux filesystem.                           |

The reasoning, since WSL makes this choice matter:

- **Imports copy, never move or modify.** Your source folders stay as they were; the
  library is an organised copy. Duplicates (same bytes) are skipped.
- **The Linux filesystem (ext4) is the default for speed.** Reading `/mnt/c` goes
  through the 9P bridge and is many times slower, and SQLite's locking isn't
  reliable there — so the database and cache always stay on the Linux side.
- **But the WSL disk is a single `ext4.vhdx` file.** If the distro is unregistered,
  that data goes with it, and Windows backup tools (File History, OneDrive) don't see
  it. So **back up `library_dir` and the database**, or put the originals on a
  Windows drive instead:

  ```yaml
  # ~/.config/phobos/config.yaml
  library_dir: /mnt/d/Phobos
  ```

  That trades some import/viewing-original speed for Windows-visible files.
  Browsing stays fast because the grid and viewer use the cache in `data_dir`.
- From Windows, the default library is at `\\wsl.localhost\<distro>\home\<you>\Phobos`
  (the Import page shows the exact path with a copy button).
- Files you add to the library folder by hand are picked up by **Rescan library**.
  Rescan also reports indexed files that went missing, and rebuilds the index if the
  database is ever lost (albums, tags and favorites live only in the DB, so back it up).

Backups:

```bash
phobos backup-db ~/backups/phobos-$(date +%F).db   # consistent copy, safe while running
rsync -a ~/Phobos/ /mnt/d/Backups/Phobos/          # originals
```

## Configuration

See [`config.example.yaml`](config.example.yaml). Copy it to `~/.config/phobos/config.yaml`
or pass `--config`. `PHOBOS_LIBRARY_DIR` / `PHOBOS_DATA_DIR` override the paths.

## Command line

```bash
phobos-web [--config FILE] [--host H] [--port P] [--reload]
phobos import "C:\Users\you\Pictures\2019"    # same as the Import page
phobos rescan
phobos info
phobos backup-db DEST
```

## Keyboard

| Where   | Keys                                                                                  |
|---------|---------------------------------------------------------------------------------------|
| Grid    | `/` search · `Ctrl+click` / `Shift+click` select · `Ctrl+A` select loaded · `Esc` clear |
| Viewer  | `←` `→` navigate · `F` favorite · `I` details · `A` add to album · `Del` trash · `Esc` close |

## How it's built

- **Backend:** FastAPI + stdlib `sqlite3` (WAL mode, numbered migrations), Pillow +
  pillow-heif for images, `ffprobe`/`ffmpeg` for video (with a built-in MP4/MOV
  parser for dates when ffprobe is missing).
- **Frontend:** plain HTML/CSS/JS modules — no framework, no build step, nothing to
  `npm install`. Native `<dialog>`, `loading="lazy"`, `IntersectionObserver`,
  `light-dark()` theming that follows the OS. Layout tuned for full and half-width
  windows next to Firefox's vertical tabs (the nav collapses to icons, and the
  viewer's details panel moves below the photo).
- **Capture dates**: EXIF → video container tags → file name patterns
  (`IMG_20240715_143012`, `PXL_…`, `Screenshot_…`, WhatsApp `IMG-20240715-WA…`) → file
  time (for browser uploads, the file's original modified time). Fix wrong ones in the
  viewer.
- **Viewer previews** are generated on demand (2048px WebP), so HEIC and TIFF display
  in any browser. Videos stream from the original with HTTP range requests (seeking
  works); codecs the browser can't decode (e.g. some HEVC) offer a download instead.
- **Security:** binds to `127.0.0.1`; the API rejects unknown `Host` headers (DNS
  rebinding) and requires an `X-Phobos` header on state-changing requests (so other
  sites open in your browser can't trigger actions).

Layout:

```
src/phobos/
  config.py     settings (YAML + env)
  db.py         SQLite connection, schema migrations, backup
  repo.py       queries (media, albums, tags, timeline, keyset pagination)
  metadata.py   EXIF / video / filename date extraction
  thumbs.py     thumbnail + preview rendering (WebP cache keyed by hash)
  library.py    import, dedupe, purge, rescan, uploads
  jobs.py       background folder imports / rescans with progress
  web/          FastAPI app, JSON API, template + static JS/CSS
tests/          pytest suite
```

## Development

```bash
make test      # pytest with coverage
make lint      # ruff check --fix
make format    # ruff format
make check     # all of the above without modifying files
```
