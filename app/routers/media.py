"""The bytes: thumbnails, full-screen images, originals, exports, videos.

Behind the same gate as /api, and reached by ``<img>`` and ``<video>`` tags,
which is why the session is a cookie (see app/security.py).

Caching is the whole performance story of a photo grid, and it rests on one
fact: a derived file's URL carries the photo's ``sig`` (``?v=``), and the sig
changes whenever the file does. Such a response can therefore be kept by the
browser forever (``immutable``), and scrolling back up costs no requests at
all. A request whose ``v`` is missing or stale still gets the current bytes,
but with ``no-cache``, so a wrong URL is never pinned. Originals have no sig in
their URL and are kept for an hour. The export is not cached at all: it
depends on the share settings, and a browser holding on to a copy made before
"strip location" was turned on is exactly the copy that must not be sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from unicodedata import normalize
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.media import (
    BROWSER_IMAGES,
    display_path,
    face_path,
    original_path,
    preview_path,
    story_path,
    thumb_path,
)
from core.models import Face, Photo

from .. import images
from ..security import require_auth
from ..serialize import export_plan, preview_ready, stem

router = APIRouter(prefix="/media", tags=["media"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]
settings = get_settings()
# Every endpoint here is a plain `def`: each one reads a row and may decode
# an image, both blocking, and FastAPI runs a `def` in its thread pool where
# that blocks nobody else.

IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"
ORIGINAL = "private, max-age=3600"
NO_STORE = "no-store"

# A JPEG or PNG under this goes to the browser as it is. Above it, a 100 MB
# drone panorama is converted like a HEIC: nobody needs 100 MB to see it.
DISPLAY_AS_IS_MAX = 25 * 1024 * 1024


def content_disposition(name: str, kind: str = "attachment") -> str:
    """RFC 6266 with the RFC 5987 ``filename*`` for anything not plain ASCII.

    Both forms, because the plain one is what old clients read and the
    starred one is what carries "Café.heic" or the fullwidth solidus iCloud
    puts in names. The plain one is an ASCII approximation: accents folded
    away, anything else that could end the header or a path replaced.
    """
    folded = normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    fallback = "".join("_" if (c in '"\\/' or ord(c) < 32 or ord(c) == 127) else c for c in folded)
    fallback = fallback.strip() or "download"
    if fallback == name:
        return f'{kind}; filename="{name}"'
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _photo(db: Session, photo_id: int) -> Photo:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "No such photo")
    return photo


def _original(photo: Photo) -> Path:
    path = original_path(photo.root, photo.rel_path)
    if path is None or not path.is_file():
        raise HTTPException(404, "The original file is not there")
    return path


def _keyed(v: str | None, photo: Photo) -> str:
    return IMMUTABLE if v and v == photo.sig else REVALIDATE


def _file(path: Path, media_type: str, cache: str, disposition: str | None = None) -> FileResponse:
    headers = {"cache-control": cache}
    if disposition:
        headers["content-disposition"] = disposition
    # FileResponse stats the file itself, but only once it is being sent, and
    # a file that vanished by then is a 500 with half a response. Stat here,
    # hand it over, and a missing file is a 404 like any other.
    try:
        stat = path.stat()
    except OSError as exc:
        raise HTTPException(404, "File not found") from exc
    return FileResponse(path, media_type=media_type, headers=headers, stat_result=stat)


@router.get("/thumb/{photo_id}")
def thumb(photo_id: int, db: DB, v: str | None = None) -> Response:
    photo = _photo(db, photo_id)
    path = thumb_path(photo.sig)
    if path.is_file():
        return _file(path, "image/webp", _keyed(v, photo))
    if photo.kind != "photo":
        # A video's frame needs ffmpeg, which is the agent's.
        raise HTTPException(404, "No thumbnail yet")
    original = _original(photo)
    try:
        data = images.render_thumb(original)
    except images.ImageError as exc:
        raise HTTPException(404, f"Cannot draw this image: {exc}") from exc
    # Made in memory and not kept: the agent's is on its way, and caching this
    # one would keep a browser showing it after the real one arrived.
    return Response(data, media_type="image/webp", headers={"cache-control": NO_STORE})


@router.get("/display/{photo_id}")
def display(photo_id: int, db: DB, v: str | None = None) -> Response:
    photo = _photo(db, photo_id)
    if photo.kind != "photo":
        raise HTTPException(404, "A video has no display image; see its thumbnail")
    original = _original(photo)
    cache = _keyed(v, photo)
    if photo.ext in BROWSER_IMAGES and photo.size < DISPLAY_AS_IS_MAX:
        return _file(original, photo.mime, cache)
    dest = display_path(photo.sig)
    if not dest.is_file():
        try:
            images.render_display(original, dest)
        except images.ImageError as exc:
            raise HTTPException(404, f"Cannot draw this image: {exc}") from exc
    return _file(dest, "image/jpeg", cache)


@router.get("/original/{photo_id}")
def original(photo_id: int, db: DB, download: bool = False) -> Response:
    photo = _photo(db, photo_id)
    path = _original(photo)
    disposition = content_disposition(photo.name) if download else None
    return _file(path, photo.mime, ORIGINAL, disposition)


@router.get("/export/{photo_id}")
def export(
    photo_id: int, db: DB, max_edge: Annotated[int, Query(alias="max")] = 0
) -> Response:
    photo = _photo(db, photo_id)
    path = _original(photo)
    if photo.kind == "video":
        _, direct = export_plan(photo)
        if not direct and preview_ready(photo):
            return _file(
                preview_path(photo.sig), "video/mp4", REVALIDATE,
                content_disposition(stem(photo.name) + ".mp4"),
            )
        return _file(path, photo.mime, REVALIDATE, content_disposition(photo.name))

    limits = [x for x in (max_edge, settings.share_max_edge) if x > 0]
    try:
        data = images.export_photo(
            path, photo.ext,
            max_edge=min(limits) if limits else 0,
            quality=settings.share_jpeg_quality,
            strip_gps=settings.share_strip_location,
        )
    except images.ImageError as exc:
        raise HTTPException(404, f"Cannot convert this image: {exc}") from exc
    if data is None:
        return _file(path, photo.mime, REVALIDATE, content_disposition(photo.name))
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "cache-control": REVALIDATE,
            "content-disposition": content_disposition(stem(photo.name) + ".jpg"),
        },
    )


@router.get("/preview/{photo_id}")
def preview(photo_id: int, db: DB, v: str | None = None) -> Response:
    photo = _photo(db, photo_id)
    if not preview_ready(photo):
        raise HTTPException(404, "No preview yet")
    # FileResponse answers Range requests itself, which is what lets a
    # <video> seek without downloading the whole file first.
    return _file(preview_path(photo.sig), "video/mp4", _keyed(v, photo))


@router.get("/story/{photo_id}")
def story(photo_id: int, db: DB, v: str | None = None) -> Response:
    """A video's strip of frames, for scrubbing on hover. The agent's to
    make (it needs ffmpeg); until it has, a 404 the tile simply ignores."""
    photo = _photo(db, photo_id)
    path = story_path(photo.sig)
    if photo.kind != "video" or not path.is_file():
        raise HTTPException(404, "No storyboard yet")
    return _file(path, "image/webp", _keyed(v, photo))


@router.get("/face/{face_id}")
def face(face_id: int, db: DB, v: str | None = None) -> Response:
    """One face's square crop, for the info panel (agent/faces.py). A photo
    looked at again numbers its faces anew, so an id outlives its crop only
    until the page asks again."""
    row = db.get(Face, face_id)
    if row is None:
        raise HTTPException(404, "No such face")
    return _file(face_path(row.sig, row.pos), "image/webp", IMMUTABLE if v and v == row.sig else REVALIDATE)
