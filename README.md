<p align="center">
  <img src="src/phobos/web/static/favicon.svg" alt="Phobos logo: a small pink moon" width="120" height="120">
</p>

<h1 align="center">Phobos</h1>

<p align="center">
  <strong>Your photos and videos, organised on your own computer.</strong><br>
  Private, simple, and no cloud: nothing ever leaves your machine.
</p>

---

Phobos is a small app that runs on your computer and opens in your web browser. Point it at
the folders where your pictures are scattered (phone backups, old drives, WhatsApp
downloads...) and it gathers them into one tidy library, sorted by the date each photo
was taken.

Your original files are never moved or changed. Phobos keeps its own organised copy.

## What you can do

- 🖼️ **Browse everything in one place**: your whole library, newest first, grouped by month.
  Jump back to any year in one click, and switch between all items, only photos or only videos.
- 🔍 **Look closer**: open any photo or video full screen and flip through with the arrow keys.
- ❤️ **Favorites, albums and tags**: keep your best shots handy and group trips, birthdays or
  anything you like. A photo can be in several albums without taking up extra space, and
  once it's in an album it leaves the main Library so you can see what's left to sort.
- 📍 **Places**: photos and videos with GPS get a place name (city, region, country), looked
  up on your own computer. Show only one country or city, or type a place in by hand.
- 🧑‍🤝‍🧑 **People & pets**: mark who is in a photo or video – any name you like, for people
  or animals – and see all of their pictures together.
- 🔎 **Search** by file name, description, place, person, tag, camera or date (try `2023-07`).
- ✂️ **Crop photos**: drag a box, pick a shape like Square or 16:9, or let Phobos trim the
  black borders around screenshots for you. Your original is kept in the Trash, just in case.
- 📊 **Metrics and cleanup**: see how big your library is and how it grew over the years, and
  find things you might not want to keep: screenshots with black borders, blank or broken
  shots, empty or tiny files, accidental 1-second videos and your largest files.
- 🧭 **No duplicates**: import the same photo twice (even under another name) and Phobos
  notices. It can show you which files it skipped and what they matched.
- 🗑️ **A safety net**: deleted items wait in the Trash until you empty it.
- 📅 **Fix wrong dates**: some files don't remember when they were taken. Correct them in a few clicks.
- ❓ **Built-in Help** for when you're not sure, and an **Exit** button (the door, top right)
  that closes Phobos when you're done.

It understands JPEG, PNG, WebP, GIF, HEIC/HEIF (iPhone photos), AVIF, TIFF and BMP, plus
MP4, MOV, WebM, MKV, AVI, 3GP and MTS videos.

## Getting started

Phobos is made for **Windows with Ubuntu on WSL**, and should feel at home on other Linux systems too.

**1. Install it** (only the first time). In the Ubuntu terminal, inside the Phobos folder:

```bash
python3 -m venv .venv && . .venv/bin/activate
make install
sudo apt install ffmpeg      # optional: thumbnails and details for videos
```

**2. Start it:**

```bash
make serve                   # or: phobos-web
```

**3. Open <http://localhost:8005>** in Firefox, Chrome or Edge on Windows.

**4. Bring in your photos** from the **Import** page:

- **Choose folder** lets you pick a folder on your computer. Your Pictures and OneDrive
  folders and every Windows drive are one click away.
- Or paste a folder path straight from Windows Explorer, like `C:\Users\you\Pictures`.
- Or just drag photos and folders onto the page.

When you're done, click the **door** in the top right corner to close Phobos. Next time,
run `make serve` again.

## Where your photos are kept

| What | Where (by default) | |
|------|--------------------|---|
| Your library | `~/Phobos` | Ordinary folders by year and month. You can open them without Phobos. |
| Albums, tags, favorites and thumbnails | `~/.local/share/phobos` | Phobos' database and picture cache. |

From Windows, the library is at `\\wsl.localhost\<your distro>\home\<you>\Phobos`. The
Import and Help pages show you the exact path.

### Please keep a backup

The Ubuntu side of WSL lives in a single virtual disk file that Windows backup tools
(File History, OneDrive) don't see. Copy your library and database somewhere safe from
time to time:

```bash
phobos backup-db ~/backups/phobos-$(date +%F).db   # safe while Phobos is running
rsync -a ~/Phobos/ /mnt/d/Backups/Phobos/          # your photos and videos
```

Prefer to keep the photos on a Windows drive? Create `~/.config/phobos/config.yaml` with:

```yaml
library_dir: /mnt/d/Phobos
```

Importing gets a little slower, but browsing stays fast. The database always stays on
the Ubuntu side, where it is quick and reliable.

Added files to the library folder by hand, or lost the database? **Rescan library** on the
Import page finds new files and rebuilds what it can. Albums, tags and favorites only
live in the database, so do back it up.

## Handy shortcuts

| Where | Keys |
|-------|------|
| Library | `/` search · `Ctrl`+click or `Shift`+click to select · `Ctrl+A` select all · `Esc` clear |
| Viewer | `←` `→` previous / next · `F` favorite · `I` details · `P` people & pets · `A` add to album · `E` crop · `Del` trash · `Esc` close |

## Settings

Every setting is optional. See [`config.example.yaml`](config.example.yaml) for the full
list (library location, port, thumbnail size, time zone...). Copy it to
`~/.config/phobos/config.yaml` or pass `--config`. `PHOBOS_LIBRARY_DIR` and
`PHOBOS_DATA_DIR` override the two paths.

Phobos also has a command line:

```bash
phobos-web [--config FILE] [--host H] [--port P] [--reload]
phobos import "C:\Users\you\Pictures\2019"    # same as the Import page
phobos rescan
phobos info
phobos backup-db DEST
```

## Privacy

Phobos only listens on your own computer (`127.0.0.1`), so other devices can't reach it.
It also refuses requests that other websites open in your browser try to send it. Your
pictures are never uploaded anywhere, and place names are looked up offline with a
built-in list of towns, so your locations never leave your computer either.

## For the curious: how it's built

- **Backend:** Python with FastAPI and SQLite. Pillow and pillow-heif handle images, and
  `ffmpeg`/`ffprobe` handle video (a built-in MP4/MOV reader finds dates when ffprobe is missing).
- **Frontend:** plain HTML, CSS and JavaScript. No framework, no build step, nothing to
  `npm install`. It follows your system's light or dark theme and works in a half-width window.
- **Dates** come from the camera's EXIF data, then the video's own tags, then patterns in the
  file name (`IMG_20240715_143012`, `PXL_…`, `Screenshot_…`, WhatsApp `IMG-20240715-WA…`),
  and finally the file's modified time.
- **Duplicates** are spotted by content (SHA-256), so renamed copies are caught too.
- **Previews** are made on demand, so HEIC and TIFF show in any browser. Videos stream with
  seeking; formats the browser can't play offer a download instead.
- **Places** come from a bundled list of ~65,000 towns
  ([GeoNames](https://www.geonames.org), CC BY 4.0; rebuild it with `scripts/build_places.py`).
  The nearest town within 100 km is used.
- **Edits** write a new file. The original is only moved to the Trash if you choose to.

```
src/phobos/
  config.py     settings (YAML + environment)
  db.py         SQLite connection, schema migrations, backup
  repo.py       queries (media, albums, tags, people, places, timeline, metrics, cleanup)
  places.py     offline GPS -> city / region / country lookup (data/ holds the town list)
  metadata.py   EXIF / video / file name date extraction
  thumbs.py     thumbnails and previews (WebP cache keyed by content hash)
  analysis.py   black-border and blank-shot detection for the cleanup tool
  library.py    import, duplicates, cropping, purge, rescan, uploads
  jobs.py       background folder imports and rescans with progress
  web/          FastAPI app, JSON API, page template and static JS/CSS
tests/          pytest suite
```

### Development

```bash
make test      # pytest with coverage
make lint      # ruff check --fix
make format    # ruff format
make check     # all of the above, without modifying files
```

## License

Phobos is free software under the [GNU General Public License v3.0](LICENSE).
Source code and issues: <https://github.com/trOOnies/phobos>.
