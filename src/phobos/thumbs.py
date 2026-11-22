"""Thumbnail and preview rendering.

Derived images live in ``<data_dir>/cache/{thumbs,previews}/<sha[:2]>/<sha>.webp``.
Keying by content hash means the cache survives a database rebuild and is
shared by duplicate imports. Everything here is safe to delete and regenerate.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from phobos import metadata as _metadata  # noqa: F401  (registers the HEIF opener)

# Formats the browsers we target display natively *and* that keep their
# behaviour (animation) only when served as-is.
PASSTHROUGH_PREVIEW_EXTS = {".gif"}


def cache_path(cache_dir: Path, variant: str, sha256: str) -> Path:
    return cache_dir / variant / sha256[:2] / f"{sha256}.webp"


def _prepare(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = img.mode in ("LA", "PA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
    return img


def _save_webp(img: Image.Image, dst: Path, quality: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(f".{os.getpid()}.tmp")
    img.save(tmp, "WEBP", quality=quality, method=4)
    tmp.replace(dst)


def _thumb_from_image(img: Image.Image, dst: Path, short_side: int) -> None:
    img = _prepare(img)
    w, h = img.size
    scale = min(1.0, short_side / min(w, h))
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    img = img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    # Tiles are centre-cropped squares: drop the far ends of very long panoramas.
    max_long = short_side * 3
    w, h = img.size
    if w > max_long:
        left = (w - max_long) // 2
        img = img.crop((left, 0, left + max_long, h))
    elif h > max_long:
        top = (h - max_long) // 2
        img = img.crop((0, top, w, top + max_long))
    _save_webp(img, dst, quality=78)


def make_image_thumb(src: Path, dst: Path, short_side: int) -> None:
    with Image.open(src) as original:
        original.draft("RGB", (short_side, short_side))  # fast JPEG DCT downscale
        _thumb_from_image(original, dst, short_side)


def make_image_preview(src: Path, dst: Path, long_side: int) -> None:
    with Image.open(src) as original:
        original.draft("RGB", (long_side, long_side))
        img = _prepare(original)
        img.thumbnail((long_side, long_side), Image.Resampling.LANCZOS, reducing_gap=3.0)
        _save_webp(img, dst, quality=85)


def grab_video_frame(src: Path, ffmpeg: str, duration: float | None) -> Image.Image:
    """Return a representative frame (ffmpeg applies rotation metadata itself)."""
    offsets = [min(1.0, duration * 0.1)] if duration else [1.0]
    offsets.append(0.0)
    for offset in offsets:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{offset:.2f}", "-i", str(src), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=120, check=False,
        )  # fmt: skip
        if proc.returncode == 0 and proc.stdout:
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()
            return img
    raise RuntimeError(f"ffmpeg could not extract a frame from {src.name}")


def make_video_thumb(
    src: Path, dst: Path, short_side: int, ffmpeg: str, duration: float | None
) -> None:
    _thumb_from_image(grab_video_frame(src, ffmpeg, duration), dst, short_side)
