from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_image, make_mp4
from phobos.web.app import create_app


def upload(
    client: TestClient, path: Path, name: str | None = None, last_modified: int | None = None
):
    data = {"last_modified": str(last_modified)} if last_modified else {}
    with path.open("rb") as fh:
        r = client.post("/api/upload", files={"file": (name or path.name, fh)}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def seeded(client: TestClient, src_dir: Path) -> list[int]:
    """Five photos (2021-01 .. 2021-05) and one video (2022-01); returns ids, newest first."""
    ids = []
    for i in range(5):
        p = make_image(
            src_dir / f"IMG_{i}.jpg",
            color=(i * 50, 0, 0),
            taken=f"2021:0{i + 1}:10 12:00:00",
            model=f"Cam{i}",
        )
        ids.append(upload(client, p)["media_id"])
    ids.append(upload(client, make_mp4(src_dir / "clip.mp4"))["media_id"])
    return ids[::-1]


def ids_of(response) -> list[int]:
    return [i["id"] for i in response.json()["items"]]


# ------------------------------------------------------------------ security


def test_mutations_require_custom_header(client: TestClient, seeded):
    r = client.post(
        "/api/media/actions", json={"ids": seeded, "action": "trash"}, headers={"X-Phobos": ""}
    )
    assert r.status_code == 403
    assert client.delete("/api/trash", headers={"X-Phobos": "0"}).status_code == 403
    # reads don't need it
    assert client.get("/api/media", headers={"X-Phobos": ""}).status_code == 200


def test_foreign_host_rejected(client: TestClient):
    assert client.get("/api/info", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/info", headers={"Host": "localhost:8005"}).status_code == 200


# ------------------------------------------------------------------ pages


def test_index_and_static(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert '<script type="module" src="/static/js/main.js">' in r.text
    js = client.get("/static/js/main.js")
    assert js.status_code == 200
    assert js.headers["cache-control"] == "no-cache"
    assert client.get("/static/css/app.css").status_code == 200


def test_info(client: TestClient, seeded):
    info = client.get("/api/info").json()
    assert info["stats"]["photos"] == 5
    assert info["stats"]["videos"] == 1
    assert info["video_thumbnails"] is False
    assert any("ffmpeg" in w for w in info["warnings"])


# ------------------------------------------------------------------ listing


def test_list_newest_first_with_pagination(client: TestClient, seeded):
    r = client.get("/api/media?limit=4")
    body = r.json()
    assert ids_of(r) == seeded[:4]
    assert body["next_cursor"]
    item = body["items"][0]
    assert item["kind"] == "video"
    assert item["thumb_url"].startswith(f"/api/media/{item['id']}/thumb?v=")
    assert item["preview_url"] == f"/api/media/{item['id']}/original"
    r2 = client.get(f"/api/media?limit=4&cursor={body['next_cursor']}")
    assert ids_of(r2) == seeded[4:]
    assert r2.json()["next_cursor"] is None
    assert client.get("/api/media?cursor=nonsense").status_code == 400


def test_filters(client: TestClient, seeded):
    assert ids_of(client.get("/api/media?kind=video")) == seeded[:1]
    assert len(ids_of(client.get("/api/media?kind=image"))) == 5
    assert ids_of(client.get("/api/media?q=cam3")) == [seeded[2]]
    assert ids_of(client.get("/api/media?q=2021-02")) == [seeded[4]]
    assert ids_of(client.get("/api/media?q=IMG_4")) == [seeded[1]]
    assert ids_of(client.get("/api/media?q=%25")) == []  # LIKE wildcards are escaped
    assert len(ids_of(client.get("/api/media?from=2021-03-01&to=2021-05-01"))) == 2


def test_timeline_and_jump_cursor(client: TestClient, seeded):
    tl = client.get("/api/timeline").json()
    assert tl[0] == {"month": "2022-01", "count": 1}
    assert [t["month"] for t in tl][1:] == ["2021-05", "2021-04", "2021-03", "2021-02", "2021-01"]
    # the UI jumps to a month with a cursor at the start of the following month
    r = client.get("/api/media?cursor=2021-04-01T00:00:00|0")
    assert ids_of(r) == seeded[3:]


# ------------------------------------------------------------------ editing


def test_patch_media(client: TestClient, seeded):
    mid = seeded[1]
    r = client.patch(
        f"/api/media/{mid}",
        json={
            "favorite": True,
            "description": "  Beach day  ",
            "tags": ["Beach", "beach", " family ", ""],
            "taken_at": "2020-12-24T21:00:00",
        },
    )
    assert r.status_code == 200
    item = r.json()
    assert item["favorite"] is True
    assert item["description"] == "Beach day"
    assert item["tags"] == ["beach", "family"]
    assert item["taken_at"] == "2020-12-24T21:00:00"
    assert item["taken_at_source"] == "manual"
    assert ids_of(client.get("/api/media?favorite=true")) == [mid]
    assert ids_of(client.get("/api/media?tag=family")) == [mid]
    assert ids_of(client.get("/api/media?q=beach")) == [mid]
    assert client.get("/api/tags").json() == [
        {"name": "beach", "count": 1},
        {"name": "family", "count": 1},
    ]
    # clearing tags prunes them
    client.patch(f"/api/media/{mid}", json={"tags": []})
    assert client.get("/api/tags").json() == []
    assert client.patch("/api/media/99999", json={"favorite": True}).status_code == 404
    assert client.get("/api/media/99999").status_code == 404


def test_bulk_favorite_and_tags(client: TestClient, seeded):
    r = client.post("/api/media/actions", json={"ids": seeded[:3], "action": "favorite"})
    assert r.json() == {"affected": 3}
    assert len(ids_of(client.get("/api/media?favorite=true"))) == 3
    client.post("/api/media/actions", json={"ids": seeded[:1], "action": "unfavorite"})
    assert len(ids_of(client.get("/api/media?favorite=true"))) == 2

    client.post("/api/media/actions", json={"ids": seeded, "action": "add_tag", "tag": "trip"})
    assert client.get("/api/tags").json() == [{"name": "trip", "count": 6}]
    client.post(
        "/api/media/actions", json={"ids": seeded[:2], "action": "remove_tag", "tag": "trip"}
    )
    assert client.get("/api/tags").json() == [{"name": "trip", "count": 4}]
    r = client.post("/api/media/actions", json={"ids": seeded, "action": "add_tag", "tag": " "})
    assert r.status_code == 422
    assert client.post("/api/media/actions", json={"ids": [], "action": "trash"}).status_code == 422


def test_trash_restore_and_purge(client: TestClient, seeded, no_ffmpeg):
    victim, keep = seeded[1], seeded[2]
    client.post("/api/media/actions", json={"ids": [victim, keep], "action": "trash"})
    assert victim not in ids_of(client.get("/api/media"))
    assert sorted(ids_of(client.get("/api/media?trash=true"))) == sorted([victim, keep])
    assert client.get("/api/info").json()["stats"]["trash"] == 2

    client.post("/api/media/actions", json={"ids": [keep], "action": "restore"})
    assert keep in ids_of(client.get("/api/media"))

    path = no_ffmpeg.library_dir / client.get(f"/api/media/{victim}").json()["rel_path"]
    assert path.exists()
    r = client.post("/api/media/actions", json={"ids": [victim, keep], "action": "purge"})
    assert r.json() == {"affected": 1}  # only trashed items can be purged
    assert not path.exists()
    assert client.get(f"/api/media/{victim}").status_code == 404

    client.post("/api/media/actions", json={"ids": [keep], "action": "trash"})
    assert client.delete("/api/trash").json() == {"affected": 1}
    assert client.get("/api/media?trash=true").json()["items"] == []


# ------------------------------------------------------------------ albums


def test_albums(client: TestClient, seeded):
    r = client.post("/api/albums", json={"name": "  Summer  ", "description": "fun"})
    assert r.status_code == 201
    album = r.json()
    assert album["name"] == "Summer"
    assert album["count"] == 0
    aid = album["id"]

    r = client.post(
        "/api/media/actions",
        json={"ids": [*seeded[:3], 99999], "action": "add_to_album", "album_id": aid},
    )
    assert r.json() == {"affected": 3}  # unknown ids are ignored
    again = client.post(
        "/api/media/actions", json={"ids": seeded[:3], "action": "add_to_album", "album_id": aid}
    )
    assert again.json() == {"affected": 0}

    album = client.get(f"/api/albums/{aid}").json()
    assert album["count"] == 3
    assert album["cover_id"] == seeded[0]  # newest item by default
    assert ids_of(client.get(f"/api/media?album={aid}")) == seeded[:3]
    assert client.get(f"/api/media/{seeded[0]}").json()["albums"] == [{"id": aid, "name": "Summer"}]

    r = client.patch(f"/api/albums/{aid}", json={"name": "Summer '21", "cover_media_id": seeded[2]})
    assert r.json()["cover_id"] == seeded[2]
    assert r.json()["name"] == "Summer '21"
    assert client.patch(f"/api/albums/{aid}", json={"cover_media_id": 99999}).status_code == 404

    # trashed items disappear from the album count and cover
    client.post("/api/media/actions", json={"ids": [seeded[2]], "action": "trash"})
    album = client.get(f"/api/albums/{aid}").json()
    assert album["count"] == 2
    assert album["cover_id"] == seeded[0]

    client.post(
        "/api/media/actions",
        json={"ids": [seeded[0]], "action": "remove_from_album", "album_id": aid},
    )
    assert client.get("/api/albums").json()[0]["count"] == 1

    missing = {"ids": seeded, "action": "add_to_album", "album_id": 424242}
    assert client.post("/api/media/actions", json=missing).status_code == 404
    no_album = {"ids": seeded, "action": "add_to_album"}
    assert client.post("/api/media/actions", json=no_album).status_code == 422

    assert client.delete(f"/api/albums/{aid}").status_code == 204
    assert client.get(f"/api/albums/{aid}").status_code == 404
    assert client.delete(f"/api/albums/{aid}").status_code == 404
    assert len(ids_of(client.get("/api/media"))) == 5  # media survive album deletion


# ------------------------------------------------------------------ files


def test_favorite_album(client: TestClient):
    first = client.post("/api/albums", json={"name": "First"}).json()
    client.post("/api/albums", json={"name": "Second"})
    assert first["favorite"] is False
    # newest first by default
    assert [a["name"] for a in client.get("/api/albums").json()] == ["Second", "First"]

    r = client.patch(f"/api/albums/{first['id']}", json={"favorite": True})
    assert r.status_code == 200
    assert r.json()["favorite"] is True
    assert r.json()["updated_at"] == first["updated_at"]  # favoriting isn't an edit
    assert client.get(f"/api/albums/{first['id']}").json()["favorite"] is True
    # favorites are listed first
    assert [a["name"] for a in client.get("/api/albums").json()] == ["First", "Second"]

    assert client.patch(f"/api/albums/{first['id']}", json={"favorite": None}).status_code == 422
    r = client.patch(f"/api/albums/{first['id']}", json={"favorite": False})
    assert r.json()["favorite"] is False
    assert [a["name"] for a in client.get("/api/albums").json()] == ["Second", "First"]


def test_item_in_several_albums_is_one_file(client: TestClient, seeded, no_ffmpeg):
    a = client.post("/api/albums", json={"name": "Beach"}).json()["id"]
    b = client.post("/api/albums", json={"name": "Family"}).json()["id"]
    for album_id in (a, b):
        r = client.post(
            "/api/media/actions",
            json={"ids": [seeded[1]], "action": "add_to_album", "album_id": album_id},
        )
        assert r.json() == {"affected": 1}
    item = client.get(f"/api/media/{seeded[1]}").json()
    assert [x["name"] for x in item["albums"]] == ["Beach", "Family"]
    assert ids_of(client.get(f"/api/media?album={a}")) == [seeded[1]]
    assert ids_of(client.get(f"/api/media?album={b}")) == [seeded[1]]
    # still a single library entry and a single file on disk
    assert client.get("/api/info").json()["stats"]["photos"] == 5
    files = [p for p in no_ffmpeg.library_dir.rglob("*") if p.is_file()]
    assert len(files) == 6
    # leaving one album keeps it in the other
    client.post(
        "/api/media/actions",
        json={"ids": [seeded[1]], "action": "remove_from_album", "album_id": a},
    )
    item = client.get(f"/api/media/{seeded[1]}").json()
    assert [x["name"] for x in item["albums"]] == ["Family"]


def test_in_album_filter(client: TestClient, seeded):
    aid = client.post("/api/albums", json={"name": "Trip"}).json()["id"]
    client.post(
        "/api/media/actions",
        json={"ids": [seeded[0], seeded[2]], "action": "add_to_album", "album_id": aid},
    )
    outside = [i for i in seeded if i not in (seeded[0], seeded[2])]
    assert ids_of(client.get("/api/media?in_album=false")) == outside
    assert ids_of(client.get("/api/media?in_album=true")) == [seeded[0], seeded[2]]
    assert ids_of(client.get("/api/media")) == seeded  # no filter: everything
    assert ids_of(client.get("/api/media?kind=video&in_album=false")) == []
    tl = client.get("/api/timeline?in_album=false").json()
    assert sum(t["count"] for t in tl) == len(outside)
    # deleting the album brings its items back to the main views
    client.delete(f"/api/albums/{aid}")
    assert ids_of(client.get("/api/media?in_album=false")) == seeded


def test_thumb_preview_original(client: TestClient, seeded):
    photo, video = seeded[1], seeded[0]
    r = client.get(f"/api/media/{photo}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]

    r = client.get(f"/api/media/{photo}/preview")
    assert r.headers["content-type"] == "image/webp"

    r = client.get(f"/api/media/{photo}/original")
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"].startswith("inline")
    r = client.get(f"/api/media/{photo}/original?download=1")
    assert r.headers["content-disposition"].startswith("attachment")
    assert "IMG_4.jpg" in r.headers["content-disposition"]

    # videos: placeholder thumbnail without ffmpeg, no preview, range requests for seeking
    r = client.get(f"/api/media/{video}/thumb")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert client.get(f"/api/media/{video}/preview").status_code == 404
    r = client.get(f"/api/media/{video}/original", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) == 10


def test_missing_original(client: TestClient, seeded, no_ffmpeg):
    item = client.get(f"/api/media/{seeded[1]}").json()
    (no_ffmpeg.library_dir / item["rel_path"]).unlink()
    assert client.get(f"/api/media/{seeded[1]}/original").status_code == 404


def test_broken_thumbnail_falls_back_to_placeholder(client: TestClient, seeded, no_ffmpeg):
    item = client.get(f"/api/media/{seeded[1]}").json()
    for p in no_ffmpeg.cache_dir.rglob("*.webp"):
        p.unlink()
    (no_ffmpeg.library_dir / item["rel_path"]).write_bytes(b"corrupted")
    r = client.get(f"/api/media/{seeded[1]}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-cache"


# ------------------------------------------------------------------ import


def test_upload_statuses(client: TestClient, src_dir: Path):
    p = make_image(src_dir / "a.jpg")
    assert upload(client, p)["status"] == "imported"
    assert upload(client, p, name="renamed.jpg")["status"] == "duplicate"
    junk = src_dir / "x.txt"
    junk.write_text("x")
    assert upload(client, junk)["status"] == "skipped"
    r = client.post("/api/upload", files={"file": ("bad.jpg", io.BytesIO(b"nope"))})
    assert r.json()["status"] == "error"


def test_folder_import_endpoint(client: TestClient, src_dir: Path, no_ffmpeg):
    make_image(src_dir / "nested/one.jpg")
    make_image(src_dir / "two.png", taken=None, model=None)
    r = client.post("/api/imports", json={"path": f'"{src_dir}"', "recursive": True})
    assert r.status_code == 202
    job_id = r.json()["id"]
    deadline = time.monotonic() + 15
    while (job := client.get(f"/api/jobs/{job_id}").json())["finished_at"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert job["status"] == "done"
    assert job["imported"] == 2
    assert [j["id"] for j in client.get("/api/jobs").json()] == [job_id]
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "done"

    assert client.post("/api/imports", json={"path": r"Q:\does\not\exist"}).status_code == 404
    inside = client.post("/api/imports", json={"path": str(no_ffmpeg.library_dir)})
    assert inside.status_code == 400
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while (job := client.get(f"/api/jobs/{job_id}").json())["finished_at"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return job


def test_folder_import_history_and_duplicates(client: TestClient, src_dir: Path, no_ffmpeg, caplog):
    a = make_image(src_dir / "a.jpg", color=(10, 20, 30))
    make_image(src_dir / "b.jpg", color=(200, 20, 30))
    first = upload(client, a)  # already in the library before the folder import
    (src_dir / "sub").mkdir()
    (src_dir / "sub/copy of a.jpg").write_bytes(a.read_bytes())

    with caplog.at_level("INFO", logger="phobos.jobs"):
        job = wait_job(
            client, client.post("/api/imports", json={"path": str(src_dir)}).json()["id"]
        )
    assert (job["imported"], job["duplicates"]) == (1, 2)
    assert f"import #{job['import_id']} started: {src_dir}" in caplog.text
    assert f"import #{job['import_id']} done: {src_dir}" in caplog.text

    history = client.get("/api/imports").json()
    entry = next(h for h in history if h["id"] == job["import_id"])
    assert entry["kind"] == "import" and entry["path"] == str(src_dir)
    assert entry["status"] == "done" and entry["finished_at"]
    assert (entry["total"], entry["imported"], entry["duplicates"]) == (3, 1, 2)

    dups = client.get(f"/api/imports/{entry['id']}/duplicates").json()
    assert dups["total"] == 2
    assert [d["source"] for d in dups["items"]] == [
        str(src_dir / "a.jpg"),
        str(src_dir / "sub/copy of a.jpg"),
    ]
    assert {d["media"]["id"] for d in dups["items"]} == {first["media_id"]}
    assert dups["items"][0]["media"]["thumb_url"].startswith(
        f"/api/media/{first['media_id']}/thumb"
    )
    page = client.get(f"/api/imports/{entry['id']}/duplicates", params={"offset": 1, "limit": 1})
    assert [d["source"] for d in page.json()["items"]] == [str(src_dir / "sub/copy of a.jpg")]

    # Deleting the matched item for good removes it from the duplicates list.
    client.post("/api/media/actions", json={"ids": [first["media_id"]], "action": "trash"})
    assert client.get(f"/api/imports/{entry['id']}/duplicates").json()["total"] == 2
    client.delete("/api/trash")
    assert client.get(f"/api/imports/{entry['id']}/duplicates").json()["total"] == 0
    assert client.get("/api/imports/999/duplicates").status_code == 404


def test_upload_session_history(client: TestClient, src_dir: Path):
    a = make_image(src_dir / "a.jpg")
    upload(client, a)
    session = client.post("/api/uploads", json={"label": "Folder “Trip” (from the browser)"})
    assert session.status_code == 201
    sid = session.json()["id"]
    for name, source in (("a.jpg", "Trip/a.jpg"), ("b.jpg", "Trip/b.jpg")):
        path = a if name == "a.jpg" else make_image(src_dir / name, color=(1, 2, 3))
        with path.open("rb") as fh:
            r = client.post(
                "/api/upload", files={"file": (name, fh)}, data={"import_id": sid, "source": source}
            )
        assert r.status_code == 200
    done = client.post(f"/api/uploads/{sid}/finish").json()
    assert done["status"] == "done" and done["kind"] == "upload"
    assert (done["total"], done["imported"], done["duplicates"]) == (2, 1, 1)
    assert done["windows_path"] is None
    dups = client.get(f"/api/imports/{sid}/duplicates").json()
    assert [d["source"] for d in dups["items"]] == ["Trip/a.jpg"]
    assert client.post("/api/uploads/999/finish").status_code == 404


def test_unfinished_imports_marked_interrupted_on_start(no_ffmpeg):
    from phobos import repo
    from phobos.library import Library

    with Library(no_ffmpeg).conn() as conn:
        stale = repo.create_import(conn, "import", "/mnt/c/Photos")
    with TestClient(create_app(no_ffmpeg), headers={"X-Phobos": "1"}) as c:
        entry = next(h for h in c.get("/api/imports").json() if h["id"] == stale)
    assert entry["status"] == "interrupted" and entry["finished_at"]
    assert entry["windows_path"] == "C:\\Photos"


def test_rescan_endpoint(client: TestClient, no_ffmpeg):
    make_image(no_ffmpeg.library_dir / "manual/added.jpg")
    job_id = client.post("/api/rescan").json()["id"]
    deadline = time.monotonic() + 15
    while (job := client.get(f"/api/jobs/{job_id}").json())["finished_at"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert job["imported"] == 1
    assert client.get("/api/media").json()["items"][0]["rel_path"] == "manual/added.jpg"


def test_extra_allowed_hosts(no_ffmpeg):
    no_ffmpeg.allowed_hosts = ["192.168.1.20"]
    with TestClient(create_app(no_ffmpeg), headers={"X-Phobos": "1"}) as c:
        assert c.get("/api/info", headers={"Host": "192.168.1.20:8005"}).status_code == 200


def test_folders_endpoint(client: TestClient, src_dir: Path, no_ffmpeg):
    (src_dir / "Trip").mkdir()
    make_image(src_dir / "a.jpg")
    r = client.get("/api/folders", params={"path": f'"{src_dir}"'})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(src_dir)
    assert body["dirs"] == [{"name": "Trip", "path": str(src_dir / "Trip")}]
    assert body["media_files"] == 1
    assert body["inside_library"] is False
    assert body["roots"][-1]["label"] == "Linux home"

    assert client.get("/api/folders", params={"path": str(no_ffmpeg.library_dir)}).json()[
        "inside_library"
    ]
    assert client.get("/api/folders", params={"path": r"Q:\nope"}).status_code == 404
    assert client.get("/api/folders", params={"path": str(src_dir / "a.jpg")}).status_code == 404
    assert client.get("/api/folders").status_code == 200  # default starting place


def test_shutdown(client: TestClient):
    calls = []
    client.app.state.stop_server = lambda: calls.append(1)
    assert client.post("/api/shutdown", headers={"X-Phobos": ""}).status_code == 403
    assert calls == []
    r = client.post("/api/shutdown")
    assert r.status_code == 202
    assert r.json() == {"stopping": True}
    assert calls == [1]  # background task ran after the response


def test_stop_server_signals(monkeypatch):
    import signal

    from phobos.web import app as app_module

    sent = []
    monkeypatch.setattr(app_module.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(app_module.os, "getpid", lambda: 100)
    monkeypatch.setattr(app_module.os, "getppid", lambda: 99)
    monkeypatch.delenv("PHOBOS_RELOAD", raising=False)
    app_module.stop_server()
    monkeypatch.setenv("PHOBOS_RELOAD", "1")
    app_module.stop_server()
    assert sent == [(100, signal.SIGTERM), (99, signal.SIGTERM)]
