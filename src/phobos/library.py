"""The media library: importing, derived images, deletion and re-indexing.

Files are never modified. Importing copies a file into
``<library_dir>/YYYY/MM/YYYYMMDD_HHMMSS_<sha8>.<ext>``; duplicates (same
SHA-256) are detected and skipped no matter what their name is. Editing a photo
writes a new file next to it (the original is only trashed if asked to).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from phobos import analysis, db, repo, thumbs
from phobos.config import Settings
from phobos.metadata import extract_metadata, local_tz
from phobos.paths import iter_media_files, library_rel_path, media_kind, mime_type

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# Formats an edited photo is saved back in; anything else (HEIC, AVIF...) becomes JPEG.
EDIT_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
                ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP"}  # fmt: skip
EXIF_ORIENTATION = 0x0112


class EditError(ValueError):
    """An edit that can't be done (shown to the user as is)."""


def _render_crop(
    src: Path, box: tuple[float, float, float, float]
) -> tuple[Image.Image, str, str, dict[str, Any]]:
    """Cropped upright image plus the format, extension and save options to write it with."""
    left, top, right, bottom = box
    with Image.open(src) as im:
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        img = ImageOps.exif_transpose(im)
    w, h = img.size
    px = (round(left * w), round(top * h), round(right * w), round(bottom * h))
    if px[2] - px[0] < 8 or px[3] - px[1] < 8:
        raise EditError("The crop area is too small.")
    if px == (0, 0, w, h):
        raise EditError("Nothing to crop: the area covers the whole picture.")
    out = img.crop(px)

    ext = src.suffix.lower()
    fmt = EDIT_FORMATS.get(ext)
    out_ext = ext if fmt else ".jpg"
    fmt = fmt or "JPEG"
    if fmt == "JPEG" and out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    exif.pop(EXIF_ORIENTATION, None)  # the pixels are upright now
    kwargs: dict[str, Any] = {}
    if fmt in ("JPEG", "PNG", "WEBP"):
        kwargs["exif"] = exif.tobytes()
    if icc and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
        kwargs["icc_profile"] = icc
    if fmt in ("JPEG", "WEBP"):
        kwargs["quality"] = 95
    return out, fmt, out_ext, kwargs


@dataclass
class ImportResult:
    status: str  # imported | duplicate | skipped | error
    name: str
    media_id: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "name": self.name, "media_id": self.media_id,
                "message": self.message}  # fmt: skip


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_dirs()
        db.init_db(settings.db_path)
        self._cache_lock = threading.Lock()
        self._rendering: dict[tuple[str, str], threading.Lock] = {}

    # ------------------------------------------------------------------ connections

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ paths

    def original_path(self, item: dict[str, Any]) -> Path:
        path = (self.settings.library_dir / item["rel_path"]).resolve()
        if not path.is_relative_to(self.settings.library_dir):
            raise ValueError("path escapes the library")
        return path

    def _cache(self, variant: str, sha256: str) -> Path:
        return thumbs.cache_path(self.settings.cache_dir, variant, sha256)

    # ------------------------------------------------------------------ importing

    def import_file(
        self,
        src: Path,
        *,
        original_name: str | None = None,
        fallback_time: datetime | None = None,
        move: bool = False,
        in_place: bool = False,
    ) -> ImportResult:
        """Import one file.

        ``move`` moves instead of copying (used for browser uploads, which are
        already a private temp copy). ``in_place`` indexes a file that already
        lives inside ``library_dir`` without touching it (used by rescan).
        """
        name = original_name or src.name
        kind = media_kind(name) or media_kind(src)
        if kind is None:
            return ImportResult("skipped", name, message="unsupported file type")
        try:
            sha = sha256_file(src)
            with self.conn() as conn:
                existing = repo.find_by_sha(conn, sha)
            if existing:
                return ImportResult("duplicate", name, existing["id"], existing["rel_path"])

            meta = extract_metadata(
                src, kind, original_name=name, fallback_time=fallback_time,
                ffprobe=self.settings.ffprobe, tz_name=self.settings.timezone,
            )  # fmt: skip
            ext = Path(name).suffix or src.suffix
            if in_place:
                rel = src.resolve().relative_to(self.settings.library_dir).as_posix()
                dest = src
            else:
                rel = library_rel_path(meta.taken_at, sha, ext)
                dest = self.settings.library_dir / rel
                self._place(src, dest, move=move)

            record = {
                "sha256": sha, "kind": kind, "rel_path": rel, "original_name": name,
                "mime": mime_type(f"x{ext}"), "size": dest.stat().st_size,
                "width": meta.width, "height": meta.height, "duration": meta.duration,
                "taken_at": meta.taken_at.isoformat(), "taken_at_source": meta.taken_at_source,
                "camera_make": meta.camera_make, "camera_model": meta.camera_model,
                "latitude": meta.latitude, "longitude": meta.longitude,
                "imported_at": repo.now_iso(),
            }  # fmt: skip
            try:
                with self.conn() as conn:
                    media_id = repo.insert_media(conn, record)
            except sqlite3.IntegrityError:
                # Lost a race against a concurrent import of the same content.
                with self.conn() as conn:
                    winner = repo.find_by_sha(conn, sha)
                if winner is None:
                    raise
                if not in_place and winner["rel_path"] != rel:
                    dest.unlink(missing_ok=True)
                return ImportResult("duplicate", name, winner["id"], winner["rel_path"])
        except Exception as exc:
            log.exception("import failed for %s", src)
            return ImportResult("error", name, message=str(exc) or exc.__class__.__name__)

        record["id"] = media_id
        try:
            self.ensure_thumb(record)
        except Exception as exc:  # the item is imported; the thumb is retried lazily
            log.warning("thumbnail failed for %s: %s", rel, exc)
        return ImportResult("imported", name, media_id, rel)

    @staticmethod
    def _place(src: Path, dest: Path, *, move: bool) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            return  # same date + hash prefix + size: already there (e.g. after a DB loss)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
        try:
            if move:
                shutil.move(src, tmp)
            else:
                shutil.copy2(src, tmp)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    def iter_import_candidates(self, root: Path, recursive: bool = True) -> Iterator[Path]:
        skip = self.settings.library_dir if root != self.settings.library_dir else None
        return iter_media_files(root, recursive=recursive, skip=skip)

    def iter_unindexed(self) -> tuple[list[Path], list[dict[str, Any]]]:
        """Files in the library missing from the DB, and DB rows whose file is gone."""
        root = self.settings.library_dir
        with self.conn() as conn:
            known = repo.all_rel_paths(conn)
            rows = [dict(r) for r in conn.execute("SELECT id, rel_path FROM media")]
        skip = self.settings.data_dir if self.settings.data_dir.is_relative_to(root) else None
        new = [
            p
            for p in iter_media_files(root, recursive=True, skip=skip)
            if p.relative_to(root).as_posix() not in known
        ]
        missing = [r for r in rows if not (root / r["rel_path"]).exists()]
        return new, missing

    # ------------------------------------------------------------------ derived images

    @contextmanager
    def _render_lock(self, variant: str, sha: str) -> Iterator[None]:
        """Serialise rendering of one image so concurrent requests don't duplicate work."""
        key = (variant, sha)
        with self._cache_lock:
            lock = self._rendering.setdefault(key, threading.Lock())
        with lock:
            yield
        with self._cache_lock:
            self._rendering.pop(key, None)

    def ensure_thumb(self, item: dict[str, Any]) -> Path | None:
        dst = self._cache("thumbs", item["sha256"])
        if dst.exists():
            return dst
        if item["kind"] == "video" and not self.settings.ffmpeg:
            return None
        with self._render_lock("thumbs", item["sha256"]):
            if not dst.exists():
                src = self.original_path(item)
                size = self.settings.thumb_size
                if item["kind"] == "video":
                    assert self.settings.ffmpeg
                    thumbs.make_video_thumb(src, dst, size, self.settings.ffmpeg, item["duration"])
                else:
                    thumbs.make_image_thumb(src, dst, size)
        return dst

    def ensure_preview(self, item: dict[str, Any]) -> Path:
        """Display-size image for the viewer (browsers can't show HEIC/TIFF)."""
        src = self.original_path(item)
        if src.suffix.lower() in thumbs.PASSTHROUGH_PREVIEW_EXTS:
            return src
        dst = self._cache("previews", item["sha256"])
        if not dst.exists():
            with self._render_lock("previews", item["sha256"]):
                if not dst.exists():
                    thumbs.make_image_preview(src, dst, self.settings.preview_size)
        return dst

    # ------------------------------------------------------------------ analysis

    def analyze(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Analyse one image (cached until its content changes)."""
        with self.conn() as conn:
            cached = repo.get_analysis(conn, item, analysis.VERSION)
        if cached is not None or item["kind"] != "image":
            return cached
        try:
            result = analysis.analyze_image(self.original_path(item))
            fields: dict[str, Any] = {"crop": result.crop, "mean": result.mean,
                                      "stddev": result.stddev}  # fmt: skip
        except FileNotFoundError:
            fields = {"error": "the file is missing from the library folder"}
        except Exception as exc:
            fields = {"error": str(exc) or exc.__class__.__name__}
        with self.conn() as conn:
            repo.save_analysis(conn, item, analysis.VERSION, **fields)
            return repo.get_analysis(conn, item, analysis.VERSION)

    def analyze_pending(self, budget_s: float = 1.5, batch: int = 100) -> dict[str, int]:
        """Analyse not-yet-analysed images for about ``budget_s`` seconds."""
        deadline = time.monotonic() + budget_s
        with self.conn() as conn:
            items = repo.pending_analysis(conn, analysis.VERSION, batch)
        done = 0
        for item in items:
            if done and time.monotonic() > deadline:
                break
            self.analyze(item)
            done += 1
        with self.conn() as conn:
            remaining = repo.count_pending_analysis(conn, analysis.VERSION)
        return {"analyzed": done, "remaining": remaining}

    # ------------------------------------------------------------------ editing

    def crop(
        self, item: dict[str, Any], box: tuple[float, float, float, float], *, replace: bool
    ) -> int:
        """Save a cropped copy of a photo and return its id.

        ``box`` is (left, top, right, bottom) as fractions of the upright picture.
        The copy gets the original's date, description, favorite, tags and albums.
        With ``replace`` the original goes to the trash (restorable until emptied).
        """
        if item["kind"] != "image":
            raise EditError("Only photos can be edited.")
        if item["deleted_at"]:
            raise EditError("Restore this photo from the trash before editing it.")
        src = self.original_path(item)
        ext = src.suffix.lower()
        if ext == ".gif":
            raise EditError("GIFs can't be edited yet.")
        left, top, right, bottom = box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise EditError("The crop area is outside the picture.")
        if not src.exists():
            raise EditError("The original file is missing from the library folder.")

        out, fmt, out_ext, kwargs = _render_crop(src, box)
        name = f"{Path(item['original_name']).stem}_cropped{out_ext}"
        tmp = self.settings.incoming_dir / f"{uuid.uuid4().hex}{out_ext}"
        try:
            out.save(tmp, fmt, **kwargs)
            result = self.import_file(
                tmp, original_name=name, move=True,
                fallback_time=datetime.fromisoformat(item["taken_at"]),
            )  # fmt: skip
        finally:
            tmp.unlink(missing_ok=True)
        if result.status not in ("imported", "duplicate") or result.media_id is None:
            raise EditError(f"Couldn't save the edited photo: {result.message or result.status}")

        with self.conn() as conn:
            if result.status == "imported":
                repo.copy_media_details(conn, item["id"], result.media_id, take_over_covers=replace)
            if replace:
                repo.trash(conn, [item["id"]])
        log.info("cropped %s -> media %s%s", item["rel_path"], result.media_id,
                 " (original trashed)" if replace else "")  # fmt: skip
        return result.media_id

    # ------------------------------------------------------------------ deletion

    def purge(self, ids: list[int] | None = None) -> int:
        """Permanently delete trashed items (all of them when ``ids`` is None)."""
        with self.conn() as conn:
            items = repo.trashed(conn, ids)
        root = self.settings.library_dir
        for item in items:
            path = self.original_path(item)
            path.unlink(missing_ok=True)
            for variant in ("thumbs", "previews"):
                self._cache(variant, item["sha256"]).unlink(missing_ok=True)
            # Tidy up emptied YYYY/MM folders, never going above the library root.
            parent = path.parent
            while parent != root and parent.is_relative_to(root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        with self.conn() as conn:
            repo.delete_rows(conn, [i["id"] for i in items])
        return len(items)

    # ------------------------------------------------------------------ uploads

    def import_upload(
        self, write: Callable[[Path], None], filename: str, last_modified_ms: int | None
    ) -> ImportResult:
        """Import a browser upload; ``write`` streams the body into the given temp path."""
        name = Path(filename).name or "upload"
        if media_kind(name) is None:
            return ImportResult("skipped", name, message="unsupported file type")
        tmp = self.settings.incoming_dir / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
        try:
            write(tmp)
            fallback = None
            if last_modified_ms:
                ts = last_modified_ms / 1000
                os.utime(tmp, (ts, ts))
                tz = local_tz(self.settings.timezone)
                fallback = datetime.fromtimestamp(ts, tz).replace(tzinfo=None)
            return self.import_file(tmp, original_name=name, fallback_time=fallback, move=True)
        finally:
            tmp.unlink(missing_ok=True)
