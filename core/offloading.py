"""Which videos are offloaded, decided in one place.

A video taken more than ``offload.after`` days ago keeps a small copy here
(480p by default, core.media.lowres_path) and nothing else: its original is
deleted once iCloud is seen to hold the very same file, and downloaded
again when somebody asks for it. The agent makes the copies and runs the
deletions and downloads (agent/offload.py); the server shows the copy and
offers the download. Both ask this module which videos it is about, so the
two cannot disagree on it.

Only what iCloud can hand back is offloaded: a video at the top of a root
that a remote is copied into, with sync on (``remote_of``). Never a Live Photo's motion, which is
part of a photo, and never a video in iCloud's Recently Deleted, which is
gone from there within thirty days. A video downloaded again stays for
``offload.keep_fetched`` days before it goes again.

The small copy is also what the video plays as, before and after the
original goes: every video has a preview, and for these this is it, so the
background conversion to 720p (``video.previews = "all"``) leaves them out.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import ColumnElement, and_, false, not_, or_, true

from .config import Settings
from .media import OFFLOAD_DIR, lowres_path, original_path
from .models import Photo
from .timeutil import utcnow


def remote_of(settings: Settings, root: str) -> str | None:
    """The remote a library root is copied from, or None when nothing is:
    ``sync.remote`` for ``sync.root``, and ``sync.remotes`` for theirs.
    Everything that fetches or offloads asks this.
    """
    if not settings.sync_enabled:
        return None
    return settings.sync_sources.get(root)


def from_remote(photo, settings: Settings) -> bool:
    """Whether this file can be downloaded again: at the top of a root that
    a remote is copied into, where ``rclone copy`` puts every file."""
    return remote_of(settings, photo.root) is not None and "/" not in photo.rel_path


def due(settings: Settings, now: datetime | None = None) -> list[ColumnElement]:
    """Where-clauses for the videos that are to live as a small copy: old
    enough, from iCloud, not on their way out of it, not downloaded again
    lately. Whether one has been offloaded yet is not part of it."""
    if not settings.offload_enabled:
        return [false()]
    now = now or utcnow()
    return [
        Photo.kind == "video",
        Photo.is_companion.is_(False),
        Photo.root.in_(list(settings.sync_sources)),
        ~Photo.rel_path.contains("/"),
        # A guessed date is still a date; no date at all is no age.
        Photo.date_source != "none",
        Photo.taken_at.is_not(None),
        Photo.taken_at < now - timedelta(days=settings.offload_after),
        Photo.icloud_deleted.is_(False),
        Photo.trashed_at.is_(None),
        or_(Photo.fetched_at.is_(None),
            Photo.fetched_at < now - timedelta(days=settings.offload_keep_fetched)),
    ]


def not_due(settings: Settings, now: datetime | None = None) -> ColumnElement:
    """The opposite of ``due``, as one clause. ``due`` never yields NULL
    (``taken_at`` is tested for it first), so NOT is an honest negation."""
    if not settings.offload_enabled:
        return true()
    return not_(and_(*due(settings, now)))


def indexed(settings: Settings) -> list[ColumnElement]:
    """Everything the original is read for, done for this version of it.

    Deleting the original first would leave these to the small copy (see
    ``source_path``), which is good enough for a frame and poorer for text.
    Embeddings and NSFW scores come from the thumbnail and the storyboard,
    which stay, so they are not waited for.
    """
    clauses = [
        Photo.meta_sig == Photo.sig,
        Photo.thumb_sig == Photo.sig,
        Photo.story_sig == Photo.sig,
    ]
    if settings.ocr_enabled and settings.ocr_videos:
        clauses.append(Photo.ocr_sig == Photo.sig)
    return clauses


def lowres_file(photo) -> Path | None:
    """The small copy of this version of the video, if one was made and is
    there. Looks at the disk: it is asked for one photo at a time."""
    if photo is None or photo.kind != "video" or not photo.lowres_sig or photo.lowres_sig != photo.sig:
        return None
    path = lowres_path(photo.root, photo.name, photo.sig)
    return path if path is not None and path.is_file() else None


def offloaded(photo) -> bool:
    """Whether the original is in iCloud only. The row says so, and the disk
    has the last word: the agent marks a row before it deletes the file."""
    if photo is None or photo.offloaded_at is None:
        return False
    if photo.rel_path.startswith(OFFLOAD_DIR + "/"):
        # Stepped aside (agent/albums.py): its path is its small copy's.
        return True
    path = original_path(photo.root, photo.rel_path)
    return path is None or not path.is_file()


def source_path(photo) -> Path | None:
    """What to read a video's frames from: its original, or for one that
    is offloaded, its small copy. ``photo`` is a row with root, rel_path,
    name, sig, kind, lowres_sig and offloaded_at."""
    path = original_path(photo.root, photo.rel_path)
    if photo.offloaded_at is None or photo.kind != "video" or (path is not None and path.is_file()):
        return path
    return lowres_file(photo) or path
