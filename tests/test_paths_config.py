from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from phobos.config import Settings, is_windows_mount, load_settings
from phobos.paths import (
    iter_media_files,
    library_rel_path,
    media_kind,
    mime_type,
    normalize_input_path,
    to_windows_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\Users\me\Pictures", "/mnt/c/Users/me/Pictures"),
        (r'"D:\Fotos\Viaje 2024\"', "/mnt/d/Fotos/Viaje 2024"),
        ("e:/camera/DCIM", "/mnt/e/camera/DCIM"),
        ("C:\\", "/mnt/c"),
        ("/mnt/c/Users/me", "/mnt/c/Users/me"),
        ("  /home/me/pics  ", "/home/me/pics"),
    ],
)
def test_normalize_input_path(raw, expected):
    assert normalize_input_path(raw) == Path(expected)


def test_normalize_input_path_expands_home():
    assert normalize_input_path("~/x") == Path.home() / "x"


def test_normalize_input_path_rejects_empty():
    with pytest.raises(ValueError):
        normalize_input_path("  ")


def test_to_windows_path():
    assert to_windows_path(Path("/mnt/d/Photos/2024")) == r"D:\Photos\2024"
    assert (
        to_windows_path(Path("/home/me/Phobos"), "Ubuntu")
        == r"\\wsl.localhost\Ubuntu\home\me\Phobos"
    )
    assert to_windows_path(Path("/home/me/Phobos"), None) is None


def test_media_kind_and_mime():
    assert media_kind("a.JPG") == "image"
    assert media_kind("clip.MOV") == "video"
    assert media_kind("notes.txt") is None
    assert mime_type("x.heic") == "image/heic"
    assert mime_type("x.mov") == "video/mp4"  # so Firefox/Chromium will try to play it
    assert mime_type("x.unknown") == "application/octet-stream"


def test_library_rel_path():
    rel = library_rel_path(datetime(2024, 7, 15, 14, 30, 12), "ab12cd34ef" + "0" * 54, ".JPG")
    assert rel == "2024/07/20240715_143012_ab12cd34.jpg"


def test_iter_media_files_skips_hidden_and_unsupported(tmp_path):
    for rel in ["a.jpg", "sub/b.mp4", ".hidden/c.jpg", "sub/.d.jpg", "notes.txt", "sub/deep/e.png"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    found = sorted(p.relative_to(tmp_path).as_posix() for p in iter_media_files(tmp_path))
    assert found == ["a.jpg", "sub/b.mp4", "sub/deep/e.png"]
    flat = [p.name for p in iter_media_files(tmp_path, recursive=False)]
    assert flat == ["a.jpg"]
    assert list(iter_media_files(tmp_path / "a.jpg")) == [tmp_path / "a.jpg"]


def test_iter_media_files_skip_dir(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.jpg").write_bytes(b"x")
    (tmp_path / "y.jpg").write_bytes(b"x")
    found = [p.name for p in iter_media_files(tmp_path, skip=tmp_path / "lib")]
    assert found == ["y.jpg"]


def test_load_settings_from_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOBOS_LIBRARY_DIR", raising=False)
    monkeypatch.delenv("PHOBOS_DATA_DIR", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"library_dir: {tmp_path}/lib\nport: 9000\nimport_workers: 8\n")
    s = load_settings(cfg)
    assert s.library_dir == tmp_path / "lib"
    assert s.port == 9000
    assert s.import_workers == 8
    assert s.db_path == s.data_dir / "phobos.db"


def test_load_settings_env_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOBOS_CONFIG", str(tmp_path / "missing-is-not-used.yaml"))
    monkeypatch.delenv("PHOBOS_CONFIG")
    monkeypatch.setenv("PHOBOS_LIBRARY_DIR", str(tmp_path / "envlib"))
    monkeypatch.setenv("PHOBOS_DATA_DIR", str(tmp_path / "envdata"))
    monkeypatch.setattr("phobos.config.DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    s = load_settings(port=1234, host=None)
    assert s.library_dir == tmp_path / "envlib"
    assert s.data_dir == tmp_path / "envdata"
    assert s.port == 1234
    assert s.host == "127.0.0.1"


def test_load_settings_rejects_unknown_keys(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("libary_dir: /typo\n")
    with pytest.raises(ValueError, match="unknown settings"):
        load_settings(cfg)


def test_warnings_for_windows_mounts(tmp_path):
    s = Settings(library_dir="/mnt/d/Photos", data_dir="/mnt/c/phobos", ffmpeg="x", ffprobe="y")
    text = " ".join(s.warnings())
    assert "data_dir" in text
    assert "library_dir" in text
    assert "ffmpeg" not in text
    assert is_windows_mount(Path("/mnt/c/x"))
    assert not is_windows_mount(Path("/mnt/wsl/x"))
    assert not is_windows_mount(tmp_path)


def test_list_folder(tmp_path):
    from phobos.paths import list_folder

    for d in ["Beta", "alpha", ".hidden", "$RECYCLE.BIN", "System Volume Information"]:
        (tmp_path / d).mkdir()
    for f in ["a.jpg", "b.MOV", "notes.txt"]:
        (tmp_path / f).write_bytes(b"x")
    listing = list_folder(tmp_path)
    assert [d["name"] for d in listing["dirs"]] == ["alpha", "Beta"]
    assert listing["dirs"][0]["path"] == str(tmp_path / "alpha")
    assert listing["media_files"] == 2
    assert listing["parent"] == str(tmp_path.parent)
    assert listing["truncated"] is False
    assert list_folder(Path("/"))["parent"] is None


def test_browse_roots(tmp_path, monkeypatch):
    from phobos.paths import browse_roots

    for rel in ["c/Users/me/Pictures", "c/Users/me/OneDrive/Imágenes", "c/Users/Public/Pictures",
                "c/Users/Default/Pictures", "d/Photos", "wsl/x", "wslg/y"]:  # fmt: skip
        (tmp_path / rel).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/me"))
    # A locked profile (e.g. the Guest account) must not hide everyone else's folders.
    locked = tmp_path / "c/Users/Guest"
    locked.mkdir()
    locked.chmod(0)
    try:
        roots = browse_roots(tmp_path)
    finally:
        locked.chmod(0o755)
    assert roots == [
        {"label": "Pictures", "path": str(tmp_path / "c/Users/me/Pictures")},
        {"label": "Imágenes (OneDrive)", "path": str(tmp_path / "c/Users/me/OneDrive/Imágenes")},
        {"label": "C:", "path": str(tmp_path / "c")},
        {"label": "D:", "path": str(tmp_path / "d")},
        {"label": "Linux home", "path": "/home/me"},
    ]
    assert browse_roots(tmp_path / "missing") == [{"label": "Linux home", "path": "/home/me"}]
