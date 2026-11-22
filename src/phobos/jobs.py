"""Background jobs (folder imports and library rescans).

Jobs run one at a time on a dedicated thread; each job fans files out to a
small worker pool (hashing, metadata and thumbnails are I/O- and C-bound, so
threads parallelise well). Live progress lives in memory; every run is also
written to the ``imports`` table (folder, counts, outcome, and the duplicates it
found) and to the log, so the import history survives restarts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from phobos import repo
from phobos.library import ImportResult, Library
from phobos.repo import now_iso

log = logging.getLogger(__name__)

MAX_REPORTED_ERRORS = 200
SAVE_EVERY = 200  # files between progress writes to the history table

JobFn = Callable[[sqlite3.Connection], None]


@dataclass
class Job:
    kind: str  # import | rescan
    path: str
    recursive: bool = True
    import_id: int | None = None  # row in the ``imports`` history table
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"  # queued | scanning | running | done | failed | cancelled
    total: int = 0
    processed: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    missing: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    cancel_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def record(self, result: ImportResult) -> None:
        self.processed += 1
        if result.status == "imported":
            self.imported += 1
        elif result.status == "duplicate":
            self.duplicates += 1
        elif result.status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
            if len(self.errors) < MAX_REPORTED_ERRORS:
                self.errors.append({"file": result.name, "error": result.message})


class JobManager:
    def __init__(self, library: Library, workers: int | None = None):
        self.library = library
        self.workers = workers or library.settings.import_workers
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phobos-job")

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        return [j for j in self.list() if j.status in ("queued", "scanning", "running")]

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job and job.status in ("queued", "scanning", "running"):
            job.cancel_requested = True
        return job

    def submit_import(self, path: Path, recursive: bool = True) -> Job:
        if not path.exists():
            raise FileNotFoundError(f"{path} does not exist")
        job = Job(kind="import", path=str(path), recursive=recursive)
        return self._submit(job, lambda conn: self._run_import(job, conn, path, recursive))

    def submit_rescan(self) -> Job:
        job = Job(kind="rescan", path=str(self.library.settings.library_dir))
        return self._submit(job, lambda conn: self._run_rescan(job, conn))

    def shutdown(self) -> None:
        for job in self.active():
            job.cancel_requested = True
        self._runner.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------------ internals

    def _submit(self, job: Job, fn: JobFn) -> Job:
        with self.library.conn() as conn:
            job.import_id = repo.create_import(conn, job.kind, job.path, recursive=job.recursive)
        log.info(
            "%s #%s started: %s%s", job.kind, job.import_id, job.path,
            "" if job.recursive else " (top folder only)",
        )  # fmt: skip
        with self._lock:
            self._jobs[job.id] = job
            # Keep the last 20 finished jobs around for the UI.
            finished = [j for j in self._jobs.values() if j.finished_at]
            for old in sorted(finished, key=lambda j: j.created_at)[:-20]:
                del self._jobs[old.id]
        self._runner.submit(self._wrap, job, fn)
        return job

    def _wrap(self, job: Job, fn: JobFn) -> None:
        with self.library.conn() as conn:
            try:
                if job.cancel_requested:
                    job.status = "cancelled"
                    return
                fn(conn)
                if job.status not in ("cancelled", "failed"):
                    job.status = "done"
            except Exception as exc:
                log.exception("job %s failed", job.id)
                job.status = "failed"
                job.message = str(exc)
            finally:
                job.finished_at = now_iso()
                self._save(conn, job, finished=True)
                log.info(
                    "%s #%s %s: %s - %d new, %d duplicates, %d skipped, %d failed",
                    job.kind, job.import_id, job.status, job.path,
                    job.imported, job.duplicates, job.skipped, job.failed,
                )  # fmt: skip

    @staticmethod
    def _save(conn: sqlite3.Connection, job: Job, *, finished: bool = False) -> None:
        """Mirror the job's counters (and final status) into the history table."""
        if job.import_id is None:
            return
        fields: dict[str, Any] = {k: getattr(job, k) for k in repo.IMPORT_COUNTERS}
        if finished:
            fields.update(status=job.status, finished_at=job.finished_at)
        try:
            repo.update_import(conn, job.import_id, **fields)
        except sqlite3.Error:  # history is best effort; never fail an import over it
            log.exception("could not update import history #%s", job.import_id)

    def _record(
        self, conn: sqlite3.Connection, job: Job, source: Path, result: ImportResult
    ) -> None:
        job.record(result)
        if result.status == "duplicate" and result.media_id and job.import_id is not None:
            try:
                repo.add_import_duplicate(conn, job.import_id, str(source), result.media_id)
            except sqlite3.Error:
                log.exception("could not record duplicate %s", source)
        if job.processed % SAVE_EVERY == 0:
            self._save(conn, job)  # progress survives a crash mid-import

    def _process(
        self, job: Job, conn: sqlite3.Connection, files: list[Path], **import_kwargs: Any
    ) -> None:
        job.total = len(files)
        job.status = "running"
        self._save(conn, job)
        with ThreadPoolExecutor(self.workers, thread_name_prefix="phobos-import") as pool:
            pending = iter(files)
            futures: dict[Future[ImportResult], Path] = {}

            def submit(path: Path) -> None:
                futures[pool.submit(self.library.import_file, path, **import_kwargs)] = path

            # Bounded submission so cancelling stops promptly on huge folders.
            for path in pending:
                submit(path)
                if len(futures) >= self.workers * 2:
                    break
            while futures:
                done = next(as_completed(futures))
                self._record(conn, job, futures.pop(done), done.result())
                if job.cancel_requested:
                    for f in futures:
                        f.cancel()
                    job.status = "cancelled"
                    return
                nxt = next(pending, None)
                if nxt is not None:
                    submit(nxt)

    def _run_import(self, job: Job, conn: sqlite3.Connection, path: Path, recursive: bool) -> None:
        job.status = "scanning"
        files: list[Path] = []
        for p in self.library.iter_import_candidates(path, recursive):
            files.append(p)
            job.total = len(files)
            if job.cancel_requested:
                job.status = "cancelled"
                return
        self._process(job, conn, files)

    def _run_rescan(self, job: Job, conn: sqlite3.Connection) -> None:
        job.status = "scanning"
        new, missing = self.library.iter_unindexed()
        job.missing = len(missing)
        if missing:
            job.message = f"{len(missing)} indexed file(s) are missing from disk"
            job.errors.extend(
                {"file": m["rel_path"], "error": "missing on disk"}
                for m in missing[:MAX_REPORTED_ERRORS]
            )
        self._process(job, conn, new, in_place=True)
