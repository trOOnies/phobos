from __future__ import annotations

import sqlite3

import yaml

from conftest import make_image
from phobos.__main__ import main


def test_cli_import_info_rescan_backup(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"library_dir": str(tmp_path / "lib"), "data_dir": str(tmp_path / "data")})
    )
    src = tmp_path / "src"
    make_image(src / "a.jpg")
    make_image(src / "b.jpg", color="blue")

    assert main(["--config", str(cfg), "import", str(src)]) == 0
    assert "2 imported" in capsys.readouterr().out

    assert main(["--config", str(cfg), "import", str(src)]) == 0
    assert "0 imported, 2 duplicates" in capsys.readouterr().out

    assert main(["--config", str(cfg), "info"]) == 0
    out = capsys.readouterr().out
    assert "photos:      2" in out

    make_image(tmp_path / "lib/extra/c.jpg", color="green")
    assert main(["--config", str(cfg), "rescan"]) == 0
    assert "1 imported" in capsys.readouterr().out

    dest = tmp_path / "backup.db"
    assert main(["--config", str(cfg), "backup-db", str(dest)]) == 0
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 3


def test_cli_reports_file_errors(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"data_dir": str(tmp_path / "d"), "library_dir": str(tmp_path / "l")})
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.jpg").write_bytes(b"nope")
    # The job itself completes; per-file errors are listed rather than aborting it.
    assert main(["--config", str(cfg), "import", str(src)]) == 0
    captured = capsys.readouterr()
    assert "1 failed" in captured.out
    assert "bad.jpg" in captured.err
