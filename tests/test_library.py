from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import make_image, make_mp4
from phobos import repo
from phobos.config import Settings
from phobos.jobs import JobManager
from phobos.library import Library, sha256_file


def wait(job, timeout=15):
    deadline = time.monotonic() + timeout
    while job.finished_at is None:
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.02)
    return job


def test_import_copies_into_dated_folder(library: Library, src_dir: Path):
    src = make_image(src_dir / "IMG_0001.JPG")
    before = src.read_bytes()
    result = library.import_file(src)
    assert result.status == "imported"
    sha = sha256_file(src)
    assert result.message == f"2023/05/20230506_070809_{sha[:8]}.jpg"
    dest = library.settings.library_dir / result.message
    assert dest.read_bytes() == before
    assert src.read_bytes() == before  # original untouched
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    assert item["original_name"] == "IMG_0001.JPG"
    assert item["taken_at"] == "2023-05-06T07:08:09"
    assert item["mime"] == "image/jpeg"
    assert item["camera_model"] == "TestCam"
    assert item["tags"] == []
    # thumbnail generated eagerly
    thumb = library.settings.cache_dir / "thumbs" / sha[:2] / f"{sha}.webp"
    assert thumb.exists()


def test_duplicates_are_detected_by_content(library: Library, src_dir: Path):
    a = make_image(src_dir / "a.jpg")
    b = src_dir / "copy-with-other-name.jpg"
    shutil.copy(a, b)
    first = library.import_file(a)
    second = library.import_file(b)
    assert first.status == "imported"
    assert second.status == "duplicate"
    assert second.media_id == first.media_id
    with library.conn() as conn:
        assert repo.stats(conn)["photos"] == 1


def test_unsupported_and_broken_files(library: Library, src_dir: Path):
    txt = src_dir / "notes.txt"
    txt.write_text("hi")
    assert library.import_file(txt).status == "skipped"
    broken = src_dir / "broken.jpg"
    broken.write_bytes(b"not an image")
    result = library.import_file(broken)
    assert result.status == "error"
    assert not any(library.settings.library_dir.rglob("*.jpg"))


def test_thumbnail_and_preview_sizes(library: Library, src_dir: Path):
    src = make_image(src_dir / "big.jpg", size=(4000, 3000), orientation=6)
    result = library.import_file(src)
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    with Image.open(library.ensure_thumb(item)) as t:
        # rotated by EXIF (portrait) and the short side matches thumb_size
        assert t.size == (360, 480)
    with Image.open(library.ensure_preview(item)) as p:
        assert max(p.size) == 2048
        assert p.size[1] > p.size[0]


def test_panorama_thumbnail_is_cropped(library: Library, src_dir: Path):
    src = make_image(src_dir / "pano.png", size=(6000, 500), taken=None, model=None)
    result = library.import_file(src)
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    with Image.open(library.ensure_thumb(item)) as t:
        assert t.size == (360 * 3, 360)


def test_gif_preview_is_passthrough(library: Library, src_dir: Path):
    src = make_image(src_dir / "anim.gif", taken=None, model=None)
    result = library.import_file(src)
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    assert library.ensure_preview(item) == library.original_path(item)


def test_video_import_without_ffmpeg(library: Library, src_dir: Path):
    src = make_mp4(src_dir / "clip.mp4")
    result = library.import_file(src)
    assert result.status == "imported"
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    assert item["kind"] == "video"
    assert item["taken_at"] == "2022-01-02T12:00:00"
    assert item["duration"] == 12.5
    assert library.ensure_thumb(item) is None  # placeholder served by the API


def test_upload_moves_temp_file_and_uses_last_modified(library: Library, src_dir: Path):
    data = make_image(src_dir / "x.png", taken=None, model=None).read_bytes()
    ms = int(time.mktime((2017, 8, 9, 10, 11, 12, 0, 0, -1)) * 1000)
    result = library.import_upload(lambda p: p.write_bytes(data), "holiday.png", ms)
    assert result.status == "imported"
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    assert item["original_name"] == "holiday.png"
    assert item["taken_at_source"] == "file"
    assert item["taken_at"].startswith("2017-08-09")
    assert not any(library.settings.incoming_dir.iterdir())


def test_upload_rejects_unsupported_names(library: Library):
    result = library.import_upload(lambda p: p.write_bytes(b"x"), "evil.exe", None)
    assert result.status == "skipped"


def test_purge_removes_files_cache_and_empty_dirs(library: Library, src_dir: Path):
    result = library.import_file(make_image(src_dir / "a.jpg"))
    with library.conn() as conn:
        item = repo.get_media(conn, result.media_id)
        library.ensure_preview(item)
        assert library.purge([item["id"]]) == 0  # not in trash yet: nothing happens
        repo.trash(conn, [item["id"]])
    assert library.purge() == 1
    root = library.settings.library_dir
    assert not (root / item["rel_path"]).exists()
    assert not (root / "2023").exists()
    assert root.exists()
    assert not list((library.settings.cache_dir).rglob("*.webp"))
    with library.conn() as conn:
        assert repo.get_media(conn, item["id"]) is None


def test_folder_import_job(library: Library, src_dir: Path):
    for i in range(6):
        make_image(
            src_dir / f"sub/IMG_{i}.jpg", color=(i * 40, 10, 10), taken=f"2021:0{i + 1}:01 10:00:00"
        )
    shutil.copy(src_dir / "sub/IMG_0.jpg", src_dir / "dupe.jpg")
    (src_dir / "readme.txt").write_text("ignored")
    (src_dir / "bad.jpg").write_bytes(b"junk")
    jobs = JobManager(library, workers=3)
    try:
        job = wait(jobs.submit_import(src_dir))
    finally:
        jobs.shutdown()
    assert job.status == "done"
    assert (job.total, job.imported, job.duplicates, job.failed) == (8, 6, 1, 1)
    assert job.errors[0]["file"] == "bad.jpg"
    with library.conn() as conn:
        months = [t["month"] for t in repo.timeline(conn, repo.MediaFilter())]
    assert months == ["2021-06", "2021-05", "2021-04", "2021-03", "2021-02", "2021-01"]


def test_concurrent_identical_files_import_once(library: Library, src_dir: Path):
    a = make_image(src_dir / "a.jpg")
    for i in range(5):
        shutil.copy(a, src_dir / f"copy{i}.jpg")
    jobs = JobManager(library, workers=4)
    try:
        job = wait(jobs.submit_import(src_dir))
    finally:
        jobs.shutdown()
    assert (job.imported, job.duplicates, job.failed) == (1, 5, 0)
    assert len(list(library.settings.library_dir.rglob("*.jpg"))) == 1


def test_cancel_job(library: Library, src_dir: Path):
    for i in range(30):
        make_image(src_dir / f"{i}.png", color=(i, i, i), taken=None, model=None)
    jobs = JobManager(library, workers=1)
    try:
        job = jobs.submit_import(src_dir)
        jobs.cancel(job.id)
        wait(job)
    finally:
        jobs.shutdown()
    assert job.status == "cancelled"
    assert job.processed < 30


def test_missing_import_path(library: Library, tmp_path: Path):
    jobs = JobManager(library)
    try:
        with pytest.raises(FileNotFoundError):
            jobs.submit_import(tmp_path / "nope")
    finally:
        jobs.shutdown()


def test_rescan_indexes_new_files_and_reports_missing(library: Library, src_dir: Path):
    kept = library.import_file(make_image(src_dir / "a.jpg"))
    gone = library.import_file(make_image(src_dir / "b.jpg", color="blue"))
    with library.conn() as conn:
        gone_item = repo.get_media(conn, gone.media_id)
    (library.settings.library_dir / gone_item["rel_path"]).unlink()
    # A file dropped into the library by hand (e.g. from Windows Explorer):
    manual = make_image(library.settings.library_dir / "Trips/beach.jpg", color="green")
    # ...and a byte-identical copy of an indexed file:
    with library.conn() as conn:
        kept_item = repo.get_media(conn, kept.media_id)
    shutil.copy(library.original_path(kept_item), library.settings.library_dir / "Trips/copy.jpg")

    jobs = JobManager(library)
    try:
        job = wait(jobs.submit_rescan())
    finally:
        jobs.shutdown()
    assert (job.imported, job.duplicates, job.missing) == (1, 1, 1)
    with library.conn() as conn:
        rel_paths = repo.all_rel_paths(conn)
    assert "Trips/beach.jpg" in rel_paths
    assert manual.exists()  # indexed in place, not moved


def test_reimport_after_db_loss_reuses_existing_file(no_ffmpeg: Settings, src_dir: Path):
    src = make_image(src_dir / "a.jpg")
    lib = Library(no_ffmpeg)
    first = lib.import_file(src)
    no_ffmpeg.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{no_ffmpeg.db_path}{suffix}").unlink(missing_ok=True)
    lib2 = Library(no_ffmpeg)
    again = lib2.import_file(src)
    assert again.status == "imported"
    assert again.message == first.message
    assert len(list(no_ffmpeg.library_dir.rglob("*.jpg"))) == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_video_thumbnail(settings: Settings, src_dir: Path):
    import subprocess

    clip = src_dir / "real.mp4"
    subprocess.run(
        [settings.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "2", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )  # fmt: skip
    lib = Library(settings)
    result = lib.import_file(clip)
    with lib.conn() as conn:
        item = repo.get_media(conn, result.media_id)
    assert item["width"] == 320
    assert item["duration"] == pytest.approx(2, abs=0.2)
    thumb = lib.ensure_thumb(item)
    assert thumb is not None
    assert thumb.exists()
