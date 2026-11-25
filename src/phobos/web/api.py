"""JSON API (mounted under ``/api``)."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator

from phobos import __version__, analysis, repo
from phobos.jobs import JobManager
from phobos.library import EditError, Library
from phobos.paths import browse_roots, list_folder, normalize_input_path, to_windows_path

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
VIDEO_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160">'
    '<rect width="160" height="160" fill="#2a2a32"/>'
    '<path d="M64 52v56l46-28z" fill="#8b8b99"/></svg>'
)
IMAGE_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160">'
    '<rect width="160" height="160" fill="#2a2a32"/>'
    '<path d="M40 112l28-36 20 24 14-16 18 28z" fill="#8b8b99"/></svg>'
)


# --------------------------------------------------------------------------- deps


def get_library(request: Request) -> Library:
    return request.app.state.library


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def get_conn(library: Library = Depends(get_library)) -> Iterator[sqlite3.Connection]:
    with library.conn() as conn:
        yield conn


LibraryDep = Annotated[Library, Depends(get_library)]
ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]
JobsDep = Annotated[JobManager, Depends(get_jobs)]


def media_filter(
    *,
    trash: bool = False,
    kind: Literal["image", "video"] | None = None,
    favorite: bool | None = None,
    album: int | None = None,
    in_album: bool | None = None,
    tag: str | None = None,
    person: int | None = None,
    country: str | None = None,
    city: str | None = None,
    q: str | None = None,
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
) -> repo.MediaFilter:
    return repo.MediaFilter(
        trash=trash, kind=kind, favorite=favorite, album_id=album, in_album=in_album, tag=tag,
        person_id=person, country=_clean(country), city=_clean(city), q=_clean(q),
        date_from=date_from, date_to=date_to,
    )  # fmt: skip


def _clean(text: str | None) -> str | None:
    return (text or "").strip() or None


FilterDep = Annotated[repo.MediaFilter, Depends(media_filter)]


def serialize(item: dict[str, Any]) -> dict[str, Any]:
    v = item["sha256"][:12]
    base = f"/api/media/{item['id']}"
    return {
        **item,
        "thumb_url": f"{base}/thumb?v={v}",
        "preview_url": f"{base}/preview?v={v}" if item["kind"] == "image" else f"{base}/original",
        "original_url": f"{base}/original",
        "download_url": f"{base}/original?download=1",
    }


def _require_media(conn: sqlite3.Connection, media_id: int) -> dict[str, Any]:
    item = repo.get_media(conn, media_id)
    if item is None:
        raise HTTPException(404, "media not found")
    return item


def _require_person(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    person = repo.get_person(conn, person_id)
    if person is None:
        raise HTTPException(404, "person not found")
    return person


def _require_album(conn: sqlite3.Connection, album_id: int) -> dict[str, Any]:
    album = repo.get_album(conn, album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    return album


# --------------------------------------------------------------------------- info


@router.get("/info")
def info(library: LibraryDep, conn: ConnDep) -> dict[str, Any]:
    s = library.settings
    distro = os.environ.get("WSL_DISTRO_NAME")
    return {
        "version": __version__,
        "library_dir": str(s.library_dir),
        "library_dir_windows": to_windows_path(s.library_dir, distro),
        "data_dir": str(s.data_dir),
        "video_thumbnails": bool(s.ffmpeg),
        "video_metadata": bool(s.ffprobe),
        "warnings": s.warnings(),
        "stats": repo.stats(conn),
    }


@router.post("/shutdown", status_code=202)
def shutdown(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """Stop the server (the UI's Exit button). Runs after the response is sent."""
    log.info("exit requested from the web UI - stopping the server")
    background.add_task(request.app.state.stop_server)
    return {"stopping": True}


# --------------------------------------------------------------------------- media


@router.get("/media")
def list_media(
    conn: ConnDep,
    flt: FilterDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    try:
        items, next_cursor = repo.list_media(conn, flt, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, "invalid cursor") from exc
    return {"items": [serialize(i) for i in items], "next_cursor": next_cursor}


@router.get("/timeline")
def timeline(conn: ConnDep, flt: FilterDep) -> list[dict[str, Any]]:
    return repo.timeline(conn, flt)


@router.get("/media/{media_id}")
def get_media(media_id: int, conn: ConnDep) -> dict[str, Any]:
    return serialize(_require_media(conn, media_id))


class PlaceIn(BaseModel):
    """A place typed in by hand; a city, a country or both."""

    city: str = Field("", max_length=100)
    region: str = Field("", max_length=100)
    country: str = Field("", max_length=100)

    @model_validator(mode="after")
    def _not_empty(self) -> PlaceIn:
        if not (repo.normalize_place(self.city) or repo.normalize_place(self.country)):
            raise ValueError("a place needs a city or a country")
        return self


class MediaPatch(BaseModel):
    favorite: bool | None = None
    description: str | None = Field(None, max_length=5000)
    taken_at: datetime | None = None
    tags: list[Annotated[str, Field(max_length=64)]] | None = None
    # A place typed in by hand; null forgets it (back to the GPS place, if any).
    place: PlaceIn | None = None


@router.patch("/media/{media_id}")
def patch_media(
    media_id: int, body: MediaPatch, conn: ConnDep, library: LibraryDep
) -> dict[str, Any]:
    _require_media(conn, media_id)
    fields: dict[str, Any] = {}
    if body.favorite is not None:
        fields["favorite"] = int(body.favorite)
    if body.description is not None:
        fields["description"] = body.description.strip()
    if body.taken_at is not None:
        fields["taken_at"] = body.taken_at.replace(tzinfo=None, microsecond=0).isoformat()
        fields["taken_at_source"] = "manual"
    repo.update_media(conn, media_id, **fields)
    if body.tags is not None:
        repo.set_tags(conn, media_id, body.tags)
    if body.place is not None:
        repo.set_place(conn, [media_id], body.place.model_dump(), "manual")
    elif "place" in body.model_fields_set:
        library.reset_place(conn, [media_id])
    return serialize(_require_media(conn, media_id))


class BulkAction(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=20000)
    action: Literal[
        "favorite", "unfavorite", "trash", "restore", "purge",
        "add_to_album", "remove_from_album", "add_tag", "remove_tag",
        "add_person", "remove_person", "set_place", "reset_place",
    ]  # fmt: skip
    album_id: int | None = None
    tag: str | None = Field(None, max_length=64)
    person_id: int | None = None
    place: PlaceIn | None = None


@router.post("/media/actions")
def bulk_action(body: BulkAction, conn: ConnDep, library: LibraryDep) -> dict[str, Any]:
    ids = body.ids
    a = body.action
    if a in ("add_to_album", "remove_from_album"):
        if body.album_id is None:
            raise HTTPException(422, "album_id is required")
        _require_album(conn, body.album_id)
    if a in ("add_tag", "remove_tag") and not (body.tag or "").strip():
        raise HTTPException(422, "tag is required")
    if a in ("add_person", "remove_person"):
        if body.person_id is None:
            raise HTTPException(422, "person_id is required")
        _require_person(conn, body.person_id)
    if a == "set_place" and body.place is None:
        raise HTTPException(422, "place is required")

    if a in ("favorite", "unfavorite"):
        n = repo.set_favorite(conn, ids, a == "favorite")
    elif a == "trash":
        n = repo.trash(conn, ids)
    elif a == "restore":
        n = repo.restore(conn, ids)
    elif a == "purge":
        n = library.purge(ids)
    elif a == "add_to_album":
        n = repo.add_to_album(conn, body.album_id, ids)  # type: ignore[arg-type]
    elif a == "remove_from_album":
        n = repo.remove_from_album(conn, body.album_id, ids)  # type: ignore[arg-type]
    elif a == "add_tag":
        n = repo.add_tag(conn, ids, body.tag)  # type: ignore[arg-type]
    elif a == "remove_tag":
        n = repo.remove_tag(conn, ids, body.tag)  # type: ignore[arg-type]
    elif a == "add_person":
        n = repo.add_person(conn, ids, body.person_id)  # type: ignore[arg-type]
    elif a == "remove_person":
        n = repo.remove_person(conn, ids, body.person_id)  # type: ignore[arg-type]
    elif a == "set_place":
        n = repo.set_place(conn, ids, body.place.model_dump(), "manual")  # type: ignore[union-attr]
    else:
        n = library.reset_place(conn, ids)
    return {"affected": n}


@router.delete("/trash")
def empty_trash(library: LibraryDep) -> dict[str, Any]:
    return {"affected": library.purge()}


def _placeholder(kind: str) -> Response:
    svg = VIDEO_PLACEHOLDER if kind == "video" else IMAGE_PLACEHOLDER
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@router.get("/media/{media_id}/thumb")
def thumb(media_id: int, conn: ConnDep, library: LibraryDep) -> Response:
    item = _require_media(conn, media_id)
    try:
        path = library.ensure_thumb(item)
    except Exception as exc:
        log.warning("thumbnail for %s failed: %s", media_id, exc)
        path = None
    if path is None:
        return _placeholder(item["kind"])
    return FileResponse(path, media_type="image/webp", headers=IMMUTABLE)


@router.get("/media/{media_id}/preview")
def preview(media_id: int, conn: ConnDep, library: LibraryDep) -> Response:
    item = _require_media(conn, media_id)
    if item["kind"] != "image":
        raise HTTPException(404, "previews exist for images only")
    try:
        path = library.ensure_preview(item)
    except FileNotFoundError as exc:
        raise HTTPException(404, "original file is missing") from exc
    except Exception as exc:
        log.warning("preview for %s failed: %s", media_id, exc)
        return _placeholder("image")
    media_type = "image/webp" if path.suffix == ".webp" else item["mime"]
    return FileResponse(path, media_type=media_type, headers=IMMUTABLE)


@router.get("/media/{media_id}/original")
def original(media_id: int, conn: ConnDep, library: LibraryDep, download: bool = False):
    item = _require_media(conn, media_id)
    path = library.original_path(item)
    if not path.exists():
        raise HTTPException(404, "original file is missing")
    return FileResponse(
        path,
        media_type=item["mime"],
        filename=item["original_name"],
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


class CropBox(BaseModel):
    """Fractions (0-1) of the upright picture."""

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)


class EditIn(BaseModel):
    crop: CropBox
    # copy: keep the original next to the edit; replace: the original goes to the trash
    mode: Literal["copy", "replace"] = "copy"


@router.post("/media/{media_id}/edit")
def edit_media(media_id: int, body: EditIn, conn: ConnDep, library: LibraryDep) -> dict[str, Any]:
    item = _require_media(conn, media_id)
    c = body.crop
    try:
        new_id = library.crop(
            item, (c.left, c.top, c.right, c.bottom), replace=body.mode == "replace"
        )
    except EditError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "media": serialize(_require_media(conn, new_id)),
        "replaced": media_id if body.mode == "replace" else None,
    }


def _analysis_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    crop = None
    if row.get("crop_left") is not None:
        crop = {k: row[f"crop_{k}"] for k in ("left", "top", "right", "bottom")}
    return {"crop": crop, "mean": row.get("mean"), "stddev": row.get("stddev"),
            "error": row.get("error")}  # fmt: skip


@router.get("/media/{media_id}/analysis")
def media_analysis(media_id: int, conn: ConnDep, library: LibraryDep) -> dict[str, Any]:
    """Black-border crop suggestion for one photo (analysed now if needed)."""
    item = _require_media(conn, media_id)
    if item["kind"] != "image":
        raise HTTPException(400, "only photos are analysed")
    return _analysis_out(library.analyze(item)) or {}


# --------------------------------------------------------------------------- metrics


@router.get("/metrics")
def metrics(conn: ConnDep, library: LibraryDep) -> dict[str, Any]:
    out = repo.metrics(conn)
    try:
        usage = shutil.disk_usage(library.settings.library_dir)
        out["disk"] = {"total": usage.total, "free": usage.free, "used": usage.used}
    except OSError:
        out["disk"] = None
    return out


def _cleanup_params(small_kb: int) -> dict[str, Any]:
    return {"version": analysis.VERSION, "small": small_kb * 1024, "blank": analysis.BLANK_STDDEV}


SmallKb = Annotated[int, Query(ge=1, le=10_000)]


@router.get("/cleanup")
def cleanup_summary(conn: ConnDep, small_kb: SmallKb = 50) -> dict[str, Any]:
    """Cleanup categories with how many items each holds right now."""
    params = _cleanup_params(small_kb)
    total = conn.execute(
        "SELECT COUNT(*) FROM media WHERE kind = 'image' AND deleted_at IS NULL AND size > 0"
    ).fetchone()[0]
    return {
        "analysis": {
            "pending": repo.count_pending_analysis(conn, analysis.VERSION),
            "total": total,
        },
        "categories": [
            {
                "id": key,
                "label": label,
                "description": desc,
                "count": repo.cleanup_count(conn, key, params),
            }
            for key, (label, desc, *_) in repo.CLEANUP.items()
        ],
    }


@router.get("/cleanup/{category}")
def cleanup_list(
    category: str,
    conn: ConnDep,
    small_kb: SmallKb = 50,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    if category not in repo.CLEANUP:
        raise HTTPException(404, "unknown cleanup category")
    params = _cleanup_params(small_kb)
    rows = repo.cleanup_items(conn, category, params, limit)
    items = []
    for row in rows:
        extra = _analysis_out(row) if "crop_left" in row else None
        media = {k: v for k, v in row.items() if k not in _ANALYSIS_KEYS}
        items.append({"media": serialize(media), "analysis": extra})
    return {"total": repo.cleanup_count(conn, category, params), "items": items}


_ANALYSIS_KEYS = {"crop_left", "crop_top", "crop_right", "crop_bottom", "mean", "stddev", "error"}


@router.post("/analysis/run")
def run_analysis(library: LibraryDep) -> dict[str, Any]:
    """Analyse the next batch of photos (~1.5 s of work); call again until remaining is 0."""
    return library.analyze_pending()


# --------------------------------------------------------------------------- tags


@router.get("/tags")
def tags(conn: ConnDep) -> list[dict[str, Any]]:
    return repo.list_tags(conn)


# --------------------------------------------------------------------------- places


@router.get("/places")
def places(conn: ConnDep) -> list[dict[str, Any]]:
    """Countries (most items first) with their cities, for the place filter."""
    return repo.list_places(conn)


# --------------------------------------------------------------------------- people & pets


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["person", "animal"] = "person"


class PersonPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    kind: Literal["person", "animal"] | None = None
    cover_media_id: int | None = None


def _name_taken(conn: sqlite3.Connection, name: str) -> HTTPException:
    existing = repo.find_person(conn, name)
    shown = existing["name"] if existing else repo.normalize_name(name)
    return HTTPException(409, f"There's already someone called “{shown}”.")


@router.get("/people")
def list_people(conn: ConnDep) -> list[dict[str, Any]]:
    return repo.list_people(conn)


@router.post("/people", status_code=201)
def create_person(body: PersonIn, conn: ConnDep) -> dict[str, Any]:
    if not repo.normalize_name(body.name):
        raise HTTPException(422, "name is required")
    try:
        person_id = repo.create_person(conn, body.name, body.kind)
    except sqlite3.IntegrityError as exc:
        raise _name_taken(conn, body.name) from exc
    return _require_person(conn, person_id)


@router.get("/people/{person_id}")
def get_person(person_id: int, conn: ConnDep) -> dict[str, Any]:
    return _require_person(conn, person_id)


@router.patch("/people/{person_id}")
def patch_person(person_id: int, body: PersonPatch, conn: ConnDep) -> dict[str, Any]:
    _require_person(conn, person_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not repo.normalize_name(fields["name"] or ""):
        raise HTTPException(422, "name is required")
    if fields.get("cover_media_id") is not None:
        _require_media(conn, fields["cover_media_id"])
    try:
        repo.update_person(conn, person_id, **fields)
    except sqlite3.IntegrityError as exc:
        raise _name_taken(conn, fields["name"]) from exc
    return _require_person(conn, person_id)


@router.delete("/people/{person_id}", status_code=204)
def delete_person(person_id: int, conn: ConnDep) -> Response:
    if not repo.delete_person(conn, person_id):
        raise HTTPException(404, "person not found")
    return Response(status_code=204)


# --------------------------------------------------------------------------- albums


class AlbumIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=5000)


class AlbumPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    cover_media_id: int | None = None


@router.get("/albums")
def list_albums(conn: ConnDep) -> list[dict[str, Any]]:
    return repo.list_albums(conn)


@router.post("/albums", status_code=201)
def create_album(body: AlbumIn, conn: ConnDep) -> dict[str, Any]:
    album_id = repo.create_album(conn, body.name.strip(), body.description.strip())
    return _require_album(conn, album_id)


@router.get("/albums/{album_id}")
def get_album(album_id: int, conn: ConnDep) -> dict[str, Any]:
    return _require_album(conn, album_id)


@router.patch("/albums/{album_id}")
def patch_album(album_id: int, body: AlbumPatch, conn: ConnDep) -> dict[str, Any]:
    _require_album(conn, album_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    if fields.get("cover_media_id") is not None:
        _require_media(conn, fields["cover_media_id"])
    repo.update_album(conn, album_id, **fields)
    return _require_album(conn, album_id)


@router.delete("/albums/{album_id}", status_code=204)
def delete_album(album_id: int, conn: ConnDep) -> Response:
    if not repo.delete_album(conn, album_id):
        raise HTTPException(404, "album not found")
    return Response(status_code=204)


# --------------------------------------------------------------------------- import


class UploadSessionIn(BaseModel):
    # What was dropped/picked: folder names when known (browsers never reveal full paths).
    label: str = Field(default="Files from the browser", max_length=1000)


@router.post("/uploads", status_code=201)
def start_upload_session(body: UploadSessionIn, conn: ConnDep) -> dict[str, Any]:
    """Open an import-history entry that a batch of browser uploads reports into."""
    label = body.label.strip() or "Files from the browser"
    import_id = repo.create_import(conn, "upload", label)
    log.info("upload #%s started: %s", import_id, label)
    return _history_row(_require_import(conn, import_id))


@router.post("/uploads/{import_id}/finish")
def finish_upload_session(import_id: int, conn: ConnDep) -> dict[str, Any]:
    row = _require_import(conn, import_id)
    if row["kind"] != "upload":
        raise HTTPException(400, "not an upload session")
    if row["finished_at"] is None:
        repo.update_import(conn, import_id, status="done", finished_at=repo.now_iso())
        row = _require_import(conn, import_id)
        log.info(
            "upload #%s done: %s - %d new, %d duplicates, %d skipped, %d failed",
            import_id, row["path"], row["imported"], row["duplicates"], row["skipped"],
            row["failed"],
        )  # fmt: skip
    return _history_row(row)


@router.post("/upload")
def upload(
    library: LibraryDep,
    file: Annotated[UploadFile, File()],
    last_modified: Annotated[int | None, Form()] = None,
    import_id: Annotated[int | None, Form()] = None,
    source: Annotated[str | None, Form(max_length=4096)] = None,
) -> dict[str, Any]:
    def write(dest: Path) -> None:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, 1024 * 1024)

    result = library.import_upload(write, file.filename or "upload", last_modified)
    if import_id is not None:
        with library.conn() as conn:
            if repo.get_import(conn, import_id) is not None:
                repo.count_import_result(conn, import_id, result.status)
                if result.status == "duplicate" and result.media_id:
                    repo.add_import_duplicate(
                        conn, import_id, source or result.name, result.media_id
                    )
    return result.as_dict()


class ImportIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    recursive: bool = True


@router.post("/imports", status_code=202)
def start_import(body: ImportIn, jobs: JobsDep, library: LibraryDep) -> dict[str, Any]:
    try:
        path = normalize_input_path(body.path).resolve()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, f"{path} does not exist (or isn't visible from Linux)")
    if path.is_relative_to(library.settings.library_dir):
        raise HTTPException(
            400, "That folder is inside the library already - use 'Rescan library' instead."
        )
    return jobs.submit_import(path, body.recursive).as_dict()


@router.get("/folders")
def folders(library: LibraryDep, path: str | None = None) -> dict[str, Any]:
    """Read-only folder listing for the import folder picker."""
    roots = browse_roots()
    try:
        target = normalize_input_path(path) if path else Path(roots[0]["path"])
        target = target.resolve()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not target.is_dir():
        raise HTTPException(404, f"{target} is not a folder (or isn't visible from Linux)")
    try:
        listing = list_folder(target)
    except PermissionError as exc:
        raise HTTPException(403, f"No permission to read {target}") from exc
    return {
        **listing,
        "windows_path": to_windows_path(target, os.environ.get("WSL_DISTRO_NAME")),
        "inside_library": target.is_relative_to(library.settings.library_dir),
        "roots": roots,
    }


def _require_import(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    row = repo.get_import(conn, import_id)
    if row is None:
        raise HTTPException(404, "import not found")
    return row


def _history_row(row: dict[str, Any]) -> dict[str, Any]:
    path = row["path"]
    distro = os.environ.get("WSL_DISTRO_NAME")
    windows = to_windows_path(Path(path), distro) if row["kind"] != "upload" else None
    return {**row, "windows_path": windows}


@router.get("/imports")
def import_history(
    conn: ConnDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[dict[str, Any]]:
    """Every import, upload batch and rescan, newest first (kept across restarts)."""
    return [_history_row(r) for r in repo.list_imports(conn, limit)]


@router.get("/imports/{import_id}/duplicates")
def import_duplicates(
    import_id: int,
    conn: ConnDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Files that run skipped because the library already had them, with the match."""
    _require_import(conn, import_id)
    total, rows = repo.import_duplicates(conn, import_id, offset=offset, limit=limit)
    return {
        "total": total,
        "offset": offset,
        "items": [{"source": r["source"], "media": serialize(r["media"])} for r in rows],
    }


@router.post("/rescan", status_code=202)
def rescan(jobs: JobsDep) -> dict[str, Any]:
    return jobs.submit_rescan().as_dict()


@router.get("/jobs")
def list_jobs(jobs: JobsDep) -> list[dict[str, Any]]:
    return [j.as_dict() for j in jobs.list()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, jobs: JobsDep) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.as_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, jobs: JobsDep) -> dict[str, Any]:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.as_dict()
