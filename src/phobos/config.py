"""Application settings.

Settings are resolved in this order (later wins):

1. Built-in defaults.
2. A YAML config file: ``--config``, else ``$PHOBOS_CONFIG``, else
   ``~/.config/phobos/config.yaml`` (only if it exists).
3. Environment variables ``PHOBOS_LIBRARY_DIR`` / ``PHOBOS_DATA_DIR``.

Two directories matter:

* ``library_dir`` holds the original files, organised as ``YYYY/MM/<file>``.
  It is plain files and folders, usable without Phobos.
* ``data_dir`` holds the SQLite database and the thumbnail/preview cache.
  It must be on a Linux filesystem (not ``/mnt/<drive>`` under WSL) because
  SQLite locking is unreliable on DrvFs and it is read constantly while browsing.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/phobos/config.yaml")


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()


@dataclass
class Settings:
    library_dir: Path = field(default_factory=lambda: Path("~/Phobos").expanduser())
    data_dir: Path = field(default_factory=lambda: _xdg_data_home() / "phobos")
    host: str = "127.0.0.1"
    port: int = 8005
    # Executables for video support; resolved from PATH when left empty.
    ffmpeg: str | None = None
    ffprobe: str | None = None
    # IANA name used to convert UTC video timestamps to local time.
    # Empty means "the system's local timezone".
    timezone: str | None = None
    thumb_size: int = 360  # short side, px (grid tiles are square crops)
    preview_size: int = 2048  # long side, px (lightbox)
    import_workers: int = 4
    # Extra Host header values to accept (e.g. a LAN IP if you bind to 0.0.0.0).
    allowed_hosts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.library_dir = Path(self.library_dir).expanduser().resolve()
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.ffmpeg = self.ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = self.ffprobe or shutil.which("ffprobe")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "phobos.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def incoming_dir(self) -> Path:
        """Scratch space for browser uploads (same FS as data_dir)."""
        return self.data_dir / "incoming"

    def ensure_dirs(self) -> None:
        for d in (self.library_dir, self.data_dir, self.cache_dir, self.incoming_dir):
            d.mkdir(parents=True, exist_ok=True)

    def warnings(self) -> list[str]:
        out = []
        if is_windows_mount(self.data_dir):
            out.append(
                f"data_dir {self.data_dir} is on a Windows drive: the database and cache "
                "will be slow and SQLite locking may misbehave. Use a Linux path."
            )
        if is_windows_mount(self.library_dir):
            out.append(
                f"library_dir {self.library_dir} is on a Windows drive: imports and "
                "opening originals will be slower (browsing uses the cache and stays fast)."
            )
        if not self.ffmpeg or not self.ffprobe:
            out.append(
                "ffmpeg/ffprobe not found: videos will import, but without thumbnails "
                "and with limited metadata. Install with: sudo apt install ffmpeg"
            )
        return out


def is_windows_mount(path: Path) -> bool:
    parts = Path(path).parts
    return len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1


def load_settings(config_path: str | Path | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {}
    path = config_path or os.environ.get("PHOBOS_CONFIG")
    if path is None and DEFAULT_CONFIG_PATH.expanduser().exists():
        path = DEFAULT_CONFIG_PATH
    if path is not None:
        path = Path(path).expanduser()
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
        known = {f.name for f in fields(Settings)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{path}: unknown settings {sorted(unknown)}")
        values.update(data)
    for env, key in (("PHOBOS_LIBRARY_DIR", "library_dir"), ("PHOBOS_DATA_DIR", "data_dir")):
        if os.environ.get(env):
            values[key] = os.environ[env]
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**values)
