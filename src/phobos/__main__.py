"""``phobos`` command line: library maintenance without the web UI."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from phobos import db, repo
from phobos.config import load_settings
from phobos.jobs import Job, JobManager
from phobos.library import Library
from phobos.paths import normalize_input_path


def _wait(manager: JobManager, job: Job) -> int:
    while job.finished_at is None:
        sys.stderr.write(
            f"\r{job.status}: {job.processed}/{job.total} "
            f"(new {job.imported}, duplicates {job.duplicates}, errors {job.failed})   "
        )
        time.sleep(0.5)
    sys.stderr.write("\n")
    for err in job.errors:
        print(f"  {err['file']}: {err['error']}", file=sys.stderr)
    print(
        f"{job.status}: {job.imported} imported, {job.duplicates} duplicates, "
        f"{job.skipped} skipped, {job.failed} failed"
        + (f", {job.missing} missing on disk" if job.missing else "")
    )
    manager.shutdown()
    return 0 if job.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phobos", description=__doc__)
    parser.add_argument("--config", help="path to a YAML config file")
    sub = parser.add_subparsers(dest="command", required=True)
    p_import = sub.add_parser("import", help="import a folder or file (Windows paths work too)")
    p_import.add_argument("path")
    p_import.add_argument("--no-recursive", action="store_true")
    sub.add_parser("rescan", help="index files added to the library folder by hand")
    sub.add_parser("info", help="show configuration and library statistics")
    p_backup = sub.add_parser("backup-db", help="write a consistent copy of the database")
    p_backup.add_argument("dest")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    library = Library(settings)

    if args.command == "info":
        print(f"library_dir: {settings.library_dir}")
        print(f"data_dir:    {settings.data_dir}")
        print(f"ffmpeg:      {settings.ffmpeg or '-'}")
        print(f"ffprobe:     {settings.ffprobe or '-'}")
        with library.conn() as conn:
            for k, v in repo.stats(conn).items():
                print(f"{k + ':':<12} {v}")
        for w in settings.warnings():
            print(f"warning: {w}", file=sys.stderr)
        return 0
    if args.command == "backup-db":
        dest = Path(args.dest).expanduser()
        db.backup(settings.db_path, dest)
        print(f"database copied to {dest}")
        return 0

    manager = JobManager(library)
    if args.command == "import":
        job = manager.submit_import(normalize_input_path(args.path), not args.no_recursive)
    else:
        job = manager.submit_rescan()
    return _wait(manager, job)


if __name__ == "__main__":
    raise SystemExit(main())
