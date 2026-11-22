"""Metadata extraction for photos and videos.

The capture date drives the whole library (timeline order and folder layout),
so it is resolved from the most to the least trustworthy source:

``exif`` -> ``video`` (container tags) -> ``filename`` -> ``file`` (mtime).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

import pillow_heif
from PIL import ExifTags, Image

pillow_heif.register_heif_opener()

log = logging.getLogger(__name__)

# EXIF tag ids
_DATETIME = 0x0132
_MAKE = 0x010F
_MODEL = 0x0110
_ORIENTATION = 0x0112
_DATETIME_ORIGINAL = 0x9003
_DATETIME_DIGITIZED = 0x9004

_FULL_STAMP = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<mo>\d{2})[-_.]?(?P<d>\d{2})"
    r"[-_ T.]?(?P<h>\d{2})[-_.:]?(?P<mi>\d{2})[-_.:]?(?P<s>\d{2})"
)
_DATE_STAMP = re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<mo>\d{2})[-_.]?(?P<d>\d{2})(?!\d)")
_ISO6709 = re.compile(r"^(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)")
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


@dataclass
class MediaMetadata:
    taken_at: datetime  # naive, local wall-clock time
    taken_at_source: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    latitude: float | None = None
    longitude: float | None = None


def local_tz(name: str | None) -> tzinfo:
    if name:
        return ZoneInfo(name)
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def _plausible(dt: datetime) -> bool:
    return datetime(1971, 1, 1) <= dt.replace(tzinfo=None) <= datetime.now() + timedelta(days=2)


# --------------------------------------------------------------------------- dates


def parse_exif_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        return dt if _plausible(dt) else None
    return None


def date_from_filename(name: str) -> datetime | None:
    """Parse common camera/phone naming schemes.

    ``IMG_20240715_143012.jpg``, ``PXL_20240715_143012345.mp4``,
    ``Screenshot_2024-07-15-14-30-12.png``, ``IMG-20240715-WA0001.jpg`` ...
    """
    stem = Path(name).stem
    for pattern in (_FULL_STAMP, _DATE_STAMP):
        for m in pattern.finditer(stem):
            g = {k: int(v) for k, v in m.groupdict().items()}
            try:
                dt = datetime(g["y"], g["mo"], g["d"], g.get("h", 0), g.get("mi", 0), g.get("s", 0))
            except ValueError:
                continue
            if _plausible(dt):
                return dt
    return None


def parse_video_datetime(value: str, tz: tzinfo) -> datetime | None:
    """Parse container timestamps. Naive/``Z`` values are UTC (per the MP4 spec)."""
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # Apple writes "2024-07-15T14:30:12-0300": add the colon fromisoformat (3.10) needs.
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz).replace(tzinfo=None, microsecond=0)
    return local if _plausible(local) else None


# --------------------------------------------------------------------------- images


def _gps_to_degrees(values: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    if str(ref).strip().upper() in ("S", "W"):
        deg = -deg
    return round(deg, 6)


def _clean(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if not isinstance(value, str):
        return None
    value = value.strip().strip("\x00").strip()
    return value or None


def read_image_metadata(path: Path) -> dict[str, Any]:
    """Raises if the file isn't a decodable image; tolerates broken EXIF."""
    with Image.open(path) as img:
        width, height = img.size
        out: dict[str, Any] = {"width": width, "height": height}
        try:
            out |= _read_exif(img, width, height)
        except Exception as exc:
            log.warning("unreadable EXIF in %s: %s", path, exc)
    return out


def _read_exif(img: Image.Image, width: int, height: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    exif = img.getexif()
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    if exif.get(_ORIENTATION) in (5, 6, 7, 8):
        width, height = height, width
    out["width"], out["height"] = width, height
    for tag in (_DATETIME_ORIGINAL, _DATETIME_DIGITIZED):
        dt = parse_exif_datetime(sub.get(tag))
        if dt:
            out["taken_at"] = dt
            break
    else:
        dt = parse_exif_datetime(exif.get(_DATETIME))
        if dt:
            out["taken_at"] = dt
    out["camera_make"] = _clean(exif.get(_MAKE))
    out["camera_model"] = _clean(exif.get(_MODEL))
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if gps and 2 in gps and 4 in gps:
        lat = _gps_to_degrees(gps[2], gps.get(1))
        lon = _gps_to_degrees(gps[4], gps.get(3))
        if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0):
            out["latitude"], out["longitude"] = lat, lon
    return out


# --------------------------------------------------------------------------- videos


def ffprobe_metadata(path: Path, ffprobe: str, tz: tzinfo) -> dict[str, Any]:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        capture_output=True, check=True, timeout=60,
    )  # fmt: skip
    data = json.loads(proc.stdout or b"{}")
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out: dict[str, Any] = {}

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video:
        w, h = video.get("width"), video.get("height")
        rotation = 0
        for sd in video.get("side_data_list") or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])
        rotation = int((video.get("tags") or {}).get("rotate", rotation))
        if w and h:
            out["width"], out["height"] = (h, w) if abs(rotation) % 180 == 90 else (w, h)
        tags = {k.lower(): v for k, v in (video.get("tags") or {}).items()} | tags
    with contextlib.suppress(KeyError, ValueError):
        out["duration"] = round(float(fmt["duration"]), 3)

    # Apple's tag carries the local offset, so it beats the UTC creation_time.
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        if key in tags:
            dt = parse_video_datetime(str(tags[key]), tz)
            if dt:
                out["taken_at"] = dt
                break
    for key in ("com.apple.quicktime.location.iso6709", "location"):
        m = _ISO6709.match(str(tags.get(key, "")))
        if m:
            out["latitude"], out["longitude"] = float(m["lat"]), float(m["lon"])
            break
    out["camera_make"] = _clean(tags.get("com.apple.quicktime.make") or tags.get("make"))
    out["camera_model"] = _clean(tags.get("com.apple.quicktime.model") or tags.get("model"))
    return out


def _iter_atoms(fh: BinaryIO, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        offset = 8
        if size == 1:
            size = struct.unpack(">Q", fh.read(8))[0]
            offset = 16
        elif size == 0:
            size = end - pos
        if size < offset:
            return
        yield kind, pos + offset, pos + size
        pos += size


def mp4_metadata(path: Path, tz: tzinfo) -> dict[str, Any]:
    """Minimal MP4/QuickTime parser (``mvhd``/``tkhd``) for when ffprobe is absent."""
    out: dict[str, Any] = {}
    with path.open("rb") as fh:
        end = fh.seek(0, 2)
        moov = next(((s, e) for k, s, e in _iter_atoms(fh, 0, end) if k == b"moov"), None)
        if moov is None:
            return out
        for kind, s, e in _iter_atoms(fh, *moov):
            if kind == b"mvhd":
                fh.seek(s)
                version = fh.read(4)[0]
                if version == 1:
                    created, _, scale, duration = struct.unpack(">QQIQ", fh.read(28))
                else:
                    created, _, scale, duration = struct.unpack(">IIII", fh.read(16))
                if created:
                    dt = (_MP4_EPOCH + timedelta(seconds=created)).astimezone(tz)
                    dt = dt.replace(tzinfo=None)
                    if _plausible(dt):
                        out["taken_at"] = dt
                if scale:
                    out["duration"] = round(duration / scale, 3)
            elif kind == b"trak" and "width" not in out:
                for k2, s2, e2 in _iter_atoms(fh, s, e):
                    if k2 != b"tkhd" or e2 - s2 < 84:
                        continue
                    fh.seek(e2 - 44)
                    matrix = struct.unpack(">9i", fh.read(36))
                    w, h = (v >> 16 for v in struct.unpack(">II", fh.read(8)))
                    if w and h:
                        # A 90/270-degree rotation matrix has a == d == 0.
                        rotated = matrix[0] == 0 and matrix[4] == 0
                        out["width"], out["height"] = (h, w) if rotated else (w, h)
    return out


# --------------------------------------------------------------------------- entry point


def _safely(fn, *args) -> dict[str, Any]:
    try:
        return fn(*args)
    except Exception as exc:  # corrupt files still get imported with what we know
        log.warning("metadata extraction failed for %s: %s", args[0], exc)
        return {}


def extract_metadata(
    path: Path,
    kind: str,
    *,
    original_name: str | None = None,
    fallback_time: datetime | None = None,
    ffprobe: str | None = None,
    tz_name: str | None = None,
) -> MediaMetadata:
    tz = local_tz(tz_name)
    info: dict[str, Any] = {}
    if kind == "image":
        info = read_image_metadata(path)  # not swallowed: undecodable images are rejected
        source = "exif"
    else:
        if ffprobe:
            info = _safely(ffprobe_metadata, path, ffprobe, tz)
        if "taken_at" not in info and path.suffix.lower() in (".mp4", ".m4v", ".mov", ".3gp"):
            found = {k: v for k, v in info.items() if v is not None}
            info = _safely(mp4_metadata, path, tz) | found
        source = "video"

    taken_at = info.pop("taken_at", None)
    if taken_at is None:
        taken_at = date_from_filename(original_name or path.name)
        source = "filename"
    if taken_at is None:
        taken_at = fallback_time or datetime.fromtimestamp(path.stat().st_mtime, tz)
        taken_at = taken_at.astimezone(tz).replace(tzinfo=None) if taken_at.tzinfo else taken_at
        source = "file"
    allowed = {"width", "height", "duration", "camera_make", "camera_model", "latitude",
               "longitude"}  # fmt: skip
    return MediaMetadata(
        taken_at=taken_at.replace(microsecond=0),
        taken_at_source=source or "file",
        **{k: v for k, v in info.items() if k in allowed},
    )
