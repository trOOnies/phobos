from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import make_image, make_mp4
from phobos import metadata
from phobos.metadata import (
    date_from_filename,
    extract_metadata,
    mp4_metadata,
    parse_exif_datetime,
    parse_video_datetime,
)

BA = ZoneInfo("America/Buenos_Aires")  # UTC-3, no DST


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_20240715_143012.jpg", datetime(2024, 7, 15, 14, 30, 12)),
        ("PXL_20240715_143012345.mp4", datetime(2024, 7, 15, 14, 30, 12)),
        ("Screenshot_2024-07-15-14-30-12.png", datetime(2024, 7, 15, 14, 30, 12)),
        ("20240715_143012.jpg", datetime(2024, 7, 15, 14, 30, 12)),
        ("VID-20240715-WA0003.mp4", datetime(2024, 7, 15)),
        ("Foto 2019-12-31.jpg", datetime(2019, 12, 31)),
        ("IMG_1234.jpg", None),
        ("20241345_999999.jpg", None),  # invalid month -> no date
        ("DSC_2999.jpg", None),
    ],
)
def test_date_from_filename(name, expected):
    assert date_from_filename(name) == expected


def test_parse_exif_datetime():
    assert parse_exif_datetime("2023:05:06 07:08:09") == datetime(2023, 5, 6, 7, 8, 9)
    assert parse_exif_datetime("2023:05:06 07:08:09\x00") == datetime(2023, 5, 6, 7, 8, 9)
    assert parse_exif_datetime("0000:00:00 00:00:00") is None
    assert parse_exif_datetime("    :  :     :  :  ") is None
    assert parse_exif_datetime(None) is None


def test_parse_video_datetime():
    # UTC "Z" is converted to local wall-clock time
    assert parse_video_datetime("2022-01-02T15:00:00.000000Z", BA) == datetime(2022, 1, 2, 12, 0)
    # Apple's local-offset format, -0300 without colon
    assert parse_video_datetime("2024-07-15T14:30:12-0300", BA) == datetime(2024, 7, 15, 14, 30, 12)
    # naive timestamps are UTC per the MP4 spec
    assert parse_video_datetime("2022-01-02 15:00:00", BA) == datetime(2022, 1, 2, 12, 0)
    assert parse_video_datetime("1904-01-01T00:00:00Z", BA) is None
    assert parse_video_datetime("garbage", BA) is None


def test_image_metadata_exif(tmp_path):
    p = make_image(tmp_path / "a.jpg", orientation=6, gps=(-34.6037, -58.3816), size=(64, 48))
    meta = extract_metadata(p, "image", tz_name="America/Buenos_Aires")
    assert meta.taken_at == datetime(2023, 5, 6, 7, 8, 9)
    assert meta.taken_at_source == "exif"
    assert (meta.width, meta.height) == (48, 64)  # rotated by EXIF orientation
    assert meta.camera_make == "Phobos"
    assert meta.camera_model == "TestCam"
    assert meta.latitude == pytest.approx(-34.6037, abs=1e-4)
    assert meta.longitude == pytest.approx(-58.3816, abs=1e-4)


def test_heic_supported(tmp_path):
    p = make_image(tmp_path / "a.heic")
    meta = extract_metadata(p, "image")
    assert meta.taken_at == datetime(2023, 5, 6, 7, 8, 9)
    assert (meta.width, meta.height) == (64, 48)


def test_image_without_exif_uses_filename_then_mtime(tmp_path):
    p = make_image(tmp_path / "IMG_20200101_101010.png", taken=None, model=None)
    meta = extract_metadata(p, "image")
    assert (meta.taken_at, meta.taken_at_source) == (datetime(2020, 1, 1, 10, 10, 10), "filename")

    q = make_image(tmp_path / "plain.png", taken=None, model=None)
    ts = datetime(2019, 3, 4, 5, 6, 7).timestamp()
    os.utime(q, (ts, ts))
    meta = extract_metadata(q, "image", tz_name=None)
    assert (meta.taken_at, meta.taken_at_source) == (datetime(2019, 3, 4, 5, 6, 7), "file")

    fallback = datetime(2018, 1, 1, 12, 0)
    meta = extract_metadata(q, "image", fallback_time=fallback)
    assert meta.taken_at == fallback


def test_original_name_used_for_filename_dates(tmp_path):
    p = make_image(tmp_path / "upload123.png", taken=None, model=None)
    meta = extract_metadata(p, "image", original_name="IMG_20210203_040506.png")
    assert meta.taken_at == datetime(2021, 2, 3, 4, 5, 6)


def test_undecodable_image_raises(tmp_path):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"definitely not a jpeg")
    with pytest.raises(Exception):  # noqa: B017 - PIL raises UnidentifiedImageError
        extract_metadata(p, "image")


def test_mp4_parser(tmp_path):
    p = make_mp4(tmp_path / "clip.mp4", rotated=True, size=(1920, 1080), duration=12.5)
    info = mp4_metadata(p, BA)
    assert info["taken_at"] == datetime(2022, 1, 2, 12, 0)  # 15:00Z -> 12:00 local
    assert info["duration"] == 12.5
    assert (info["width"], info["height"]) == (1080, 1920)


def test_video_without_ffprobe_uses_mp4_parser(tmp_path):
    p = make_mp4(tmp_path / "clip.mov")
    meta = extract_metadata(p, "video", ffprobe=None, tz_name="America/Buenos_Aires")
    assert meta.taken_at_source == "video"
    assert meta.taken_at == datetime(2022, 1, 2, 12, 0)
    assert (meta.width, meta.height) == (1920, 1080)


def test_video_without_creation_time_falls_back(tmp_path):
    p = make_mp4(tmp_path / "VID_20210607_080910.mp4", created=None)
    meta = extract_metadata(p, "video", ffprobe=None)
    assert (meta.taken_at, meta.taken_at_source) == (datetime(2021, 6, 7, 8, 9, 10), "filename")
    assert meta.duration == 12.5


def test_garbage_video_still_gets_a_date(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"\x00\x00\x00\x01junk")
    meta = extract_metadata(p, "video", ffprobe=None)
    assert meta.taken_at_source == "file"


def test_ffprobe_metadata_parsing(tmp_path, monkeypatch):
    probe = {
        "format": {
            "duration": "3.504",
            "tags": {
                "creation_time": "2024-07-15T17:30:12.000000Z",
                "com.apple.quicktime.creationdate": "2024-07-15T14:30:12-0300",
                "com.apple.quicktime.location.ISO6709": "-34.6037-058.3816+025.000/",
                "com.apple.quicktime.make": "Apple",
                "com.apple.quicktime.model": "iPhone 15",
            },
        },
        "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "width": 1920, "height": 1080,
             "side_data_list": [{"rotation": -90}]},
        ],
    }  # fmt: skip

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "/usr/bin/ffprobe"
        return subprocess.CompletedProcess(cmd, 0, json.dumps(probe).encode(), b"")

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)
    p = tmp_path / "IMG_0001.MOV"
    p.write_bytes(b"")
    meta = extract_metadata(p, "video", ffprobe="/usr/bin/ffprobe", tz_name="UTC")
    # The Apple tag (with local offset) wins: 14:30-03:00 == 17:30 UTC.
    assert meta.taken_at == datetime(2024, 7, 15, 17, 30, 12)
    assert meta.taken_at_source == "video"
    assert (meta.width, meta.height) == (1080, 1920)
    assert meta.duration == 3.504
    assert (meta.latitude, meta.longitude) == (-34.6037, -58.3816)
    assert (meta.camera_make, meta.camera_model) == ("Apple", "iPhone 15")


def test_ffprobe_failure_falls_back_to_parser(tmp_path, monkeypatch):
    def failing_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(metadata.subprocess, "run", failing_run)
    p = make_mp4(tmp_path / "clip.mp4", created=datetime(2020, 5, 5, 5, 5, 5, tzinfo=timezone.utc))
    meta = extract_metadata(p, "video", ffprobe="ffprobe", tz_name="UTC")
    assert meta.taken_at == datetime(2020, 5, 5, 5, 5, 5)
