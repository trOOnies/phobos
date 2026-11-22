"""Filesystem helpers: supported formats, WSL path handling, library naming."""

from __future__ import annotations

import contextlib
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
}  # fmt: skip
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp", ".mts", ".m2ts", ".wmv",
}  # fmt: skip

MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif",
    # QuickTime files from phones are almost always H.264/HEVC in an MP4-compatible
    # container; announcing them as video/mp4 lets Firefox and Chromium try to play them.
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".3gp": "video/3gpp", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
}  # fmt: skip

_WIN_PATH = re.compile(r"^(?P<drive>[a-zA-Z]):[\\/]*(?P<rest>.*)$")


def media_kind(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def mime_type(path: str | Path) -> str:
    return MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def normalize_input_path(raw: str) -> Path:
    r"""Turn a user-typed path into a Linux path.

    Accepts Windows paths pasted from Explorer (``C:\Users\me\Pictures`` ->
    ``/mnt/c/Users/me/Pictures``), quoted paths, and ``~``.
    """
    text = raw.strip().strip('"').strip("'").strip()
    if not text:
        raise ValueError("empty path")
    m = _WIN_PATH.match(text)
    if m:
        rest = m.group("rest").replace("\\", "/").strip("/")
        return Path(PurePosixPath("/mnt", m.group("drive").lower(), rest))
    return Path(text).expanduser()


def to_windows_path(path: Path, distro: str | None = None) -> str | None:
    r"""How Windows Explorer can reach ``path`` (``/mnt/d/x`` -> ``D:\x``).

    Linux paths go through the ``\\wsl.localhost\<distro>`` share. Returns None
    outside WSL.
    """
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1:
        return f"{parts[2].upper()}:\\" + "\\".join(parts[3:])
    if not distro:
        return None
    return f"\\\\wsl.localhost\\{distro}" + "\\".join(("", *parts[1:]))


def library_rel_path(taken_at: datetime, sha256: str, ext: str) -> str:
    """Destination of an imported file, relative to the library root.

    ``2024/07/20240715_143012_ab12cd34.jpg`` - sortable, human-browsable, and
    collision-free thanks to the content hash suffix.
    """
    stamp = taken_at.strftime("%Y%m%d_%H%M%S")
    return f"{taken_at:%Y}/{taken_at:%m}/{stamp}_{sha256[:8]}{ext.lower()}"


def iter_media_files(root: Path, *, recursive: bool = True, skip: Path | None = None):
    """Yield supported media files under ``root`` (hidden entries are skipped)."""
    if root.is_file():
        if media_kind(root):
            yield root
        return
    pattern = "**/*" if recursive else "*"
    for p in sorted(root.glob(pattern)):
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if skip is not None and p.is_relative_to(skip):
            continue
        if p.is_file() and media_kind(p):
            yield p


# --------------------------------------------------------------------------- folder browser

_SKIP_DIRS = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws"}
_SKIP_USERS = {"public", "default", "default user", "all users"}
# "Pictures" as Explorer names it on disk in common Windows languages.
_PICTURES_NAMES = (
    "Pictures",
    "Imágenes",
    "Images",
    "Bilder",
    "Immagini",
    "Imagens",
    "Afbeeldingen",
)
MAX_LISTED_DIRS = 2000


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that treats unreadable entries (e.g. locked Windows profiles) as absent."""
    try:
        return path.is_dir()
    except OSError:
        return False


def browse_roots(mnt: Path = Path("/mnt")) -> list[dict[str, str]]:
    """Starting points for the folder picker: Windows Pictures folders, drives, home."""
    roots: list[dict[str, str]] = []
    drives = sorted(
        p for p in (mnt.iterdir() if _is_dir(mnt) else []) if len(p.name) == 1 and _is_dir(p)
    )
    for drive in drives:
        users = drive / "Users"
        if not _is_dir(users):
            continue
        try:
            for user in sorted(users.iterdir()):
                if user.name.lower() in _SKIP_USERS:
                    continue
                roots += _pictures_folders(user)
        except OSError:
            continue
    roots += [{"label": f"{d.name.upper()}:", "path": str(d)} for d in drives]
    roots.append({"label": "Linux home", "path": str(Path.home())})
    return roots


def _pictures_folders(user: Path) -> list[dict[str, str]]:
    """The user's Pictures folder(s), including OneDrive-redirected and localised ones."""
    bases = [(user, "")]
    with contextlib.suppress(OSError):
        bases += [(d, f" ({d.name})") for d in sorted(user.glob("OneDrive*")) if _is_dir(d)]
    found = []
    for base, suffix in bases:
        for name in _PICTURES_NAMES:
            pics = base / name
            if _is_dir(pics):
                found.append({"label": f"{name}{suffix}", "path": str(pics)})
                break
    return found


def list_folder(path: Path) -> dict:
    """Subfolders of ``path`` and how many supported media files sit directly in it."""
    dirs: list[dict[str, str]] = []
    media_files = 0
    with os.scandir(path) as it:
        for entry in it:
            name = entry.name
            if name.startswith((".", "$")) or name.lower() in _SKIP_DIRS:
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": name, "path": entry.path})
                elif entry.is_file() and media_kind(name):
                    media_files += 1
            except OSError:  # broken links, permission problems on DrvFs
                continue
    dirs.sort(key=lambda d: d["name"].casefold())
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "dirs": dirs[:MAX_LISTED_DIRS],
        "truncated": len(dirs) > MAX_LISTED_DIRS,
        "media_files": media_files,
    }
