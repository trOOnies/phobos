"""Image analysis for the cleanup tool: black borders that can be cropped, blank shots.

Results are cached per media item in ``media_analysis`` and keyed by the file's
SHA-256, so they stay valid until the content changes. Analysis works on a
downscaled copy (JPEG DCT draft + box filter), which is fast and averages out
compression noise along the borders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from phobos import metadata as _metadata  # noqa: F401  (registers the HEIF opener)

VERSION = 1
WORK_SIZE = 1024  # long side of the analysed copy
DARK = 24  # luma at or below this counts as "black background"
MIN_TRIM = 0.08  # suggest a crop only when it removes at least 8% of the picture
MIN_KEEP = 0.05  # ... and leaves at least 5% of each dimension
BLANK_STDDEV = 6.0  # luma standard deviation below this is "almost a single colour"


@dataclass
class Analysis:
    # Content box as fractions of the (orientation-corrected) picture, or None
    # when there is no black border worth cropping.
    crop: tuple[float, float, float, float] | None
    mean: float
    stddev: float


def analyze_image(path: Path) -> Analysis:
    with Image.open(path) as im:
        im.draft("L", (WORK_SIZE, WORK_SIZE))
        img = ImageOps.exif_transpose(im).convert("L")
    img.thumbnail((WORK_SIZE, WORK_SIZE), Image.Resampling.BOX)
    stat = ImageStat.Stat(img)
    mean, stddev = stat.mean[0], stat.stddev[0]

    w, h = img.size
    bbox = img.point(lambda v: 255 if v > DARK else 0).getbbox()
    crop = None
    if bbox is not None and stddev >= BLANK_STDDEV:
        left, top, right, bottom = bbox
        kept = (right - left) * (bottom - top) / (w * h)
        if (
            1 - kept >= MIN_TRIM
            and (right - left) >= w * MIN_KEEP
            and (bottom - top) >= h * MIN_KEEP
        ):
            crop = (left / w, top / h, right / w, bottom / h)
    return Analysis(crop=crop, mean=round(mean, 2), stddev=round(stddev, 2))
