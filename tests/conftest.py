from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from phobos.config import Settings
from phobos.library import Library

MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def make_image(
    path: Path,
    *,
    taken: str | None = "2023:05:06 07:08:09",
    size: tuple[int, int] = (64, 48),
    color: str | tuple[int, int, int] = "red",
    orientation: int | None = None,
    gps: tuple[float, float] | None = None,
    model: str | None = "TestCam",
) -> Path:
    """Write an image with EXIF; format follows the suffix (.jpg, .png, .heic ...)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    if model:
        exif[0x010F] = "Phobos"
        exif[0x0110] = model
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif.get_ifd(ExifTags.IFD.Exif)[0x9003] = taken
    if gps:
        lat, lon = gps

        def dms(v: float) -> tuple[float, float, float]:
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            return (float(d), float(m), round((v - d - m / 60) * 3600, 4))

        exif.get_ifd(ExifTags.IFD.GPSInfo).update(
            {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}
        )
    kwargs = {"exif": exif.tobytes()} if path.suffix.lower() != ".png" or taken or model else {}
    img.save(path, **kwargs)
    return path


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def make_mp4(
    path: Path,
    *,
    created: datetime | None = datetime(2022, 1, 2, 15, 0, 0, tzinfo=timezone.utc),
    duration: float = 12.5,
    size: tuple[int, int] = (1920, 1080),
    rotated: bool = False,
) -> Path:
    """Minimal MP4 skeleton (ftyp, moov/mvhd, trak/tkhd, mdat) for metadata parsing."""
    secs = int((created - MP4_EPOCH).total_seconds()) if created else 0
    timescale = 1000
    mvhd = struct.pack(">B3sIIII", 0, b"\0\0\0", secs, secs, timescale, int(duration * timescale))
    mvhd += b"\0" * 80
    if rotated:  # 90 degree matrix: a=0, b=1, c=-1, d=0
        matrix = (0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)
    else:
        matrix = (0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = struct.pack(">B3sIIIIIQhhhh", 0, b"\0\0\x03", secs, secs, 1, 0, 0, 0, 0, 0, 0, 0)
    tkhd += struct.pack(">9i", *matrix) + struct.pack(">II", size[0] << 16, size[1] << 16)
    moov = _atom(b"moov", _atom(b"mvhd", mvhd) + _atom(b"trak", _atom(b"tkhd", tkhd)))
    data = _atom(b"ftyp", b"isom\0\0\x02\0isomiso2mp41") + _atom(b"mdat", b"\0" * 64) + moov
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        library_dir=tmp_path / "library",
        data_dir=tmp_path / "data",
        timezone="America/Buenos_Aires",
        ffmpeg="",
        ffprobe="",
        import_workers=2,
    )


@pytest.fixture
def no_ffmpeg(settings: Settings) -> Settings:
    settings.ffmpeg = None
    settings.ffprobe = None
    return settings


@pytest.fixture
def library(no_ffmpeg: Settings) -> Library:
    return Library(no_ffmpeg)


@pytest.fixture
def client(no_ffmpeg: Settings):
    # Imported here so the core tests run without the optional [web] dependencies.
    from fastapi.testclient import TestClient

    from phobos.web.app import create_app

    app = create_app(no_ffmpeg)
    with TestClient(app, headers={"X-Phobos": "1"}) as c:
        yield c


@pytest.fixture
def src_dir(tmp_path: Path) -> Path:
    d = tmp_path / "source"
    d.mkdir()
    return d
