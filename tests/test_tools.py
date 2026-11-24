"""Photo editing (crop)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import make_image, make_mp4


def upload(client: TestClient, path: Path) -> int:
    with path.open("rb") as fh:
        r = client.post("/api/upload", files={"file": (path.name, fh)})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "imported", r.json()
    return r.json()["media_id"]


def textured(path: Path, size=(400, 300), box=None) -> Path:
    """A busy picture; with ``box`` it sits on a black background (like a screenshot)."""
    w, h = size
    img = Image.new("RGB", size, "black")
    x0, y0, x1, y1 = box or (0, 0, w, h)
    draw = ImageDraw.Draw(img)
    for x in range(x0, x1, 10):
        for y in range(y0, y1, 10):
            draw.rectangle((x, y, x + 9, y + 9), fill=(60 + x % 190, 80 + y % 170, 200))
    img.save(path, quality=95)
    return path


@pytest.fixture
def items(client: TestClient, src_dir: Path) -> dict[str, int]:
    return {
        "bordered": upload(client, textured(src_dir / "shot.jpg", box=(100, 100, 300, 200))),
        "plain": upload(client, textured(src_dir / "plain.jpg")),
        "black": upload(client, make_image(src_dir / "black.png", color="black", model=None)),
        "clip": upload(client, make_mp4(src_dir / "clip.mp4", duration=1.2)),
    }


# ------------------------------------------------------------------ editing


def test_crop_replace_keeps_details_and_trashes_original(client: TestClient, items, no_ffmpeg):
    src = items["bordered"]
    aid = client.post("/api/albums", json={"name": "Shots"}).json()["id"]
    client.post(
        "/api/media/actions", json={"ids": [src], "action": "add_to_album", "album_id": aid}
    )
    client.patch(f"/api/albums/{aid}", json={"cover_media_id": src})
    client.patch(f"/api/media/{src}", json={"favorite": True, "description": "hi", "tags": ["x"],
                                            "taken_at": "2019-03-04T05:06:07"})  # fmt: skip

    r = client.post(f"/api/media/{src}/edit", json={
        "crop": {"left": 0.25, "top": 1 / 3, "right": 0.75, "bottom": 2 / 3}, "mode": "replace",
    })  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    new = body["media"]
    assert body["replaced"] == src
    assert (new["width"], new["height"]) == (200, 100)
    assert new["original_name"] == "shot_cropped.jpg"
    assert new["taken_at"] == "2019-03-04T05:06:07"
    assert new["taken_at_source"] == "manual"
    assert new["favorite"] is True and new["description"] == "hi" and new["tags"] == ["x"]
    assert [a["id"] for a in new["albums"]] == [aid]
    assert client.get(f"/api/albums/{aid}").json()["cover_id"] == new["id"]
    assert client.get(f"/api/media/{src}").json()["deleted_at"]  # original in the trash, on disk
    assert (no_ffmpeg.library_dir / client.get(f"/api/media/{src}").json()["rel_path"]).exists()
    with Image.open(no_ffmpeg.library_dir / new["rel_path"]) as im:
        assert im.size == (200, 100)

    # editing a trashed photo is refused
    again = client.post(
        f"/api/media/{src}/edit", json={"crop": {"left": 0, "top": 0, "right": 0.5, "bottom": 0.5}}
    )
    assert again.status_code == 400


def test_crop_copy_respects_orientation(client: TestClient, src_dir: Path):
    # 64x48 pixels stored sideways (EXIF orientation 6): upright it is 48 wide, 64 tall
    mid = upload(client, make_image(src_dir / "turned.jpg", orientation=6))
    r = client.post(
        f"/api/media/{mid}/edit", json={"crop": {"left": 0, "top": 0, "right": 1, "bottom": 0.5}}
    )
    assert r.status_code == 200, r.text
    new = r.json()["media"]
    assert r.json()["replaced"] is None
    assert (new["width"], new["height"]) == (48, 32)
    assert client.get(f"/api/media/{mid}").json()["deleted_at"] is None  # copy: original stays


@pytest.mark.parametrize(
    ("crop", "status"),
    [
        ({"left": 0, "top": 0, "right": 1, "bottom": 1}, 400),  # nothing to crop
        ({"left": 0.6, "top": 0, "right": 0.5, "bottom": 1}, 400),  # inside out
        ({"left": 0, "top": 0, "right": 1.5, "bottom": 1}, 422),  # outside the picture
        ({"left": 0, "top": 0, "right": 0.01, "bottom": 0.01}, 400),  # too small
    ],
)
def test_crop_validation(client: TestClient, items, crop, status):
    r = client.post(f"/api/media/{items['plain']}/edit", json={"crop": crop})
    assert r.status_code == status, r.text


def test_crop_refuses_videos_and_gifs(client: TestClient, items, src_dir: Path):
    box = {"left": 0, "top": 0, "right": 0.5, "bottom": 0.5}
    assert client.post(f"/api/media/{items['clip']}/edit", json={"crop": box}).status_code == 400
    gif = src_dir / "anim.gif"
    Image.new("RGB", (40, 40), "blue").save(gif)
    gid = upload(client, gif)
    assert client.post(f"/api/media/{gid}/edit", json={"crop": box}).status_code == 400
    assert client.post("/api/media/424242/edit", json={"crop": box}).status_code == 404
