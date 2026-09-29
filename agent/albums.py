"""What iCloud knows about each photo that the file does not.

A heart set on the phone, the Hidden album, Recently Deleted, the day a
picture arrived, which albums it is in: none of it is in the file ``rclone
copy`` brings down, and all of it is in the listing. rclone's iCloud Photos
backend shows every album as a folder beside "All Photos", and
``rclone lsjson --metadata`` gives per file its size, its time and a few
iCloud fields (``favorite``, ``hidden``, ``added-time``). So a refresh is one
listing per album, matched against the library, written in one transaction.

Matching a listed file to a local one is by exact name, which is what
``rclone copy`` keeps. Not always: when two assets share a name, "All
Photos" (and so the local folder) gives both an iCloud id suffix,
``IMG_0886_AUJZT...HEIC``, while an album may list the same asset as plain
``IMG_0886.HEIC``. Such an entry is matched by size, time to the second and
extension, among local names that start with its stem and ``_``.

Listing everything takes about a minute ("All Photos" is most of it), is read
only, and changes nothing until every album has been read: a listing that
fails half way (an expired session, the network) leaves the database as it
was. It runs after every sync, when the UI asks, and at startup when the last
refresh is more than a day old; never beside a sync or another refresh (the
agent runs them one at a time, see agent/main.py).

The same listing keeps the names of photos deleted here honest (see
core.models.DeletedFile): a name that "All Photos" now lists with another
size and time is another photo's, one no sync should leave out.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from core.config import Settings
from core.database import SessionLocal
from core.models import SMART_ALBUMS, Album, DeletedFile, Photo, PhotoAlbum
from core.timeutil import utcnow

from . import jobs, rclone
from .log import log
from .sync import auth_hint

FAVORITES = "Favorites"
HIDDEN = "Hidden"
DELETED = "Recently Deleted"
# "All Photos" takes about a minute for 25,000 photos; a hung connection
# should not hold the agent's remote slot for longer than this.
LIST_TIMEOUT = 900.0
STALE_AFTER = timedelta(hours=24)
NAME_LIMIT = 300                # Album.name


class AlbumsError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


# --- the listing -------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    mtime: int | None               # whole seconds since the epoch
    favorite: bool = False
    hidden: bool = False
    added: datetime | None = None   # naive UTC


def remote_path(parent: str, name: str) -> str:
    return f"{parent}{name}" if parent.endswith(":") else f"{parent.rstrip('/')}/{name}"


def main_album(settings: Settings) -> str:
    """The album ``sync.remote`` copies from ("All Photos")."""
    return settings.sync_remote.rstrip("/").rsplit("/", 1)[-1].split(":")[-1]


def parse_dirs(text: str) -> list[str]:
    """``rclone lsf --dirs-only``: one folder per line, each ending in ``/``."""
    out = []
    for line in text.splitlines():
        name = line.rstrip("\r").rstrip("/")
        if name and name not in out:
            out.append(name)
    return out


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _instant(text) -> datetime | None:
    """An RFC 3339 time as an aware datetime, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = _FRACTION.sub(r"\1", text.strip()).replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def epoch_seconds(text) -> int | None:
    dt = _instant(text)
    return int(dt.timestamp() // 1) if dt else None


def naive_utc(text) -> datetime | None:
    dt = _instant(text)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None


def _true(value) -> bool:
    return str(value).strip().lower() == "true"


def parse_entries(text: str) -> list[Entry]:
    """``rclone lsjson --metadata --files-only``, as entries."""
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise AlbumsError(f"rclone's listing is not JSON: {exc}") from exc
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("IsDir"):
            continue
        name = str(item.get("Name") or item.get("Path") or "").rsplit("/", 1)[-1]
        if not name:
            continue
        size = item.get("Size")
        meta = item.get("Metadata") if isinstance(item.get("Metadata"), dict) else {}
        out.append(Entry(
            name=name,
            size=size if isinstance(size, int) and not isinstance(size, bool) else -1,
            mtime=epoch_seconds(item.get("ModTime")),
            favorite=_true(meta.get("favorite")),
            hidden=_true(meta.get("hidden")),
            added=naive_utc(meta.get("added-time")),
        ))
    return out


# --- matching ------------------------------------------------------------------------


def _split(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    return (stem, ext.lower()) if dot else (name, "")


class Matcher:
    """Listed entries -> ids of rows at the top of the sync root (where
    ``rclone copy`` puts every file)."""

    def __init__(self, rows: Iterable):
        self.by_name: dict[str, object] = {}
        self.by_size: dict[int, list] = defaultdict(list)
        for row in rows:
            self.by_name[row.name] = row
            self.by_size[row.size].append(row)

    def match(self, entry: Entry) -> int | None:
        exact = self.by_name.get(entry.name)
        if exact is not None and exact.size == entry.size:
            return exact.id
        if entry.mtime is not None and entry.size >= 0:
            stem, ext = _split(entry.name)
            for row in self.by_size.get(entry.size, ()):
                other, other_ext = _split(row.name)
                if (
                    row is not exact and other_ext == ext and other.startswith(stem + "_")
                    # rclone sets the local mtime to iCloud's, and lists it
                    # truncated to the second.
                    and abs(row.mtime_ns // 1_000_000_000 - entry.mtime) <= 1
                ):
                    return row.id
        # Same name, different size: the local copy is older or newer than
        # the listing, and still the same photo.
        return exact.id if exact is not None else None


# --- writing it down -----------------------------------------------------------------


@dataclass
class Outcome:
    listed: dict[str, int] = field(default_factory=dict)        # album -> entries
    matched: dict[str, int] = field(default_factory=dict)       # album -> local photos
    changed: int = 0                                            # photo rows updated
    released: int = 0                                           # deleted names now another photo's

    def summary(self) -> str:
        n = len(self.listed)
        text = f"{n} album" + ("" if n == 1 else "s")
        user = {name: count for name, count in self.matched.items() if name not in SMART_ALBUMS}
        # The one album this app gives a place of its own (is:whatsapp);
        # else the biggest of the user's.
        pick = next((name for name in user if name.lower() == "whatsapp"), None)
        if pick is None and user:
            pick = max(user, key=lambda name: (user[name], name))
        if pick is not None:
            text += f", {user[pick]:,} in {pick}"
        return text


def apply(db: Session, settings: Settings, listings: dict[str, list[Entry]],
          now: datetime | None = None) -> Outcome:
    """Write one complete refresh. Commits; nothing is written on an error."""
    now = now or utcnow()
    rows = db.execute(
        select(Photo.id, Photo.name, Photo.rel_path, Photo.size, Photo.mtime_ns, Photo.favorite,
               Photo.hidden, Photo.icloud_deleted, Photo.added_at)
        .where(Photo.root == settings.sync_root)
    ).all()
    matcher = Matcher(r for r in rows if "/" not in r.rel_path)
    main = main_album(settings)

    members: dict[str, set[int]] = {}
    favorite: set[int] = set()
    hidden: set[int] = set()
    added_main: dict[int, datetime] = {}
    added_other: dict[int, datetime] = {}
    outcome = Outcome()
    for name, entries in listings.items():
        ids: set[int] = set()
        for entry in entries:
            photo_id = matcher.match(entry)
            if photo_id is None:
                continue
            ids.add(photo_id)
            if entry.favorite:
                favorite.add(photo_id)
            if entry.hidden:
                hidden.add(photo_id)
            if entry.added is not None:
                (added_main if name == main else added_other).setdefault(photo_id, entry.added)
        members[name] = ids
        outcome.listed[name] = len(entries)
        outcome.matched[name] = len(ids)
    favorite |= members.get(FAVORITES, set())
    hidden |= members.get(HIDDEN, set())
    deleted = members.get(DELETED, set())

    changes = []
    for r in rows:
        want = {
            "favorite": r.id in favorite,
            "hidden": r.id in hidden,
            "icloud_deleted": r.id in deleted,
            # A photo no longer listed anywhere keeps the day it arrived.
            "added_at": added_main.get(r.id) or added_other.get(r.id) or r.added_at,
        }
        if any(getattr(r, key) != value for key, value in want.items()):
            changes.append({"id": r.id, **want})
    if changes:
        db.execute(update(Photo), changes)
    outcome.changed = len(changes)

    existing = {a.name: a for a in db.scalars(select(Album))}
    for name in listings:
        album = existing.get(name)
        if album is None:
            album = Album(name=name)
            db.add(album)
            existing[name] = album
        album.kind = "smart" if name in SMART_ALBUMS else "user"
        album.count = outcome.matched[name]
        album.remote_count = outcome.listed[name]
        album.synced_at = now
    for name, album in existing.items():
        if name not in listings:
            db.delete(album)
    db.flush()
    if main in listings:
        outcome.released = release_names(db, settings, listings[main])
    # iCloud is the truth: membership is rebuilt, not patched.
    db.execute(delete(PhotoAlbum))
    pairs = [{"album_id": existing[name].id, "photo_id": photo_id}
             for name, ids in members.items() for photo_id in sorted(ids)]
    if pairs:
        db.execute(insert(PhotoAlbum), pairs)
    db.commit()
    return outcome


def release_names(db: Session, settings: Settings, entries: list[Entry]) -> int:
    """Stop excluding a deleted photo's name once iCloud lists a different
    photo under it: a new one that took the plain name after the deleted one
    left iCloud too. The deleted photo is still known by its content. Returns
    how many names were given up."""
    listed = {entry.name: entry for entry in entries}
    released = 0
    for note in db.scalars(select(DeletedFile).where(
        DeletedFile.root == settings.sync_root, DeletedFile.name.is_not(None)
    )):
        entry = listed.get(note.name)
        if entry is None or entry.size < 0 or entry.size == note.size:
            continue
        # A size alone can differ for the same photo (see Matcher.match); a
        # time a second or more apart as well cannot.
        if entry.mtime is not None and abs(note.mtime_ns // 1_000_000_000 - entry.mtime) <= 1:
            continue
        log(f"albums: {note.name} in iCloud is another photo now than the one deleted here; syncing it")
        note.name = None
        released += 1
    return released


def last_refresh(db: Session) -> datetime | None:
    return db.scalar(select(func.max(Album.synced_at)))


def startup_due(db: Session, settings: Settings, now: datetime | None = None) -> bool:
    """Whether the agent should refresh at startup: never refreshed, or not
    for a day (the phone changes favourites without any sync noticing)."""
    if not (settings.sync_enabled and settings.sync_albums):
        return False
    last = last_refresh(db)
    return last is None or (now or utcnow()) - last >= STALE_AFTER


# --- the job -------------------------------------------------------------------------


class AlbumsRun:
    """One refresh for one job, reported into the job's row."""

    def __init__(self, job_id: int, settings: Settings, *,
                 echo: Callable[[str], None] | None = None):
        self.job_id = job_id
        self.settings = settings
        self.echo = echo
        self.status = "running"
        self._stopped = False
        self._cancelled = False
        self._last_cancel_check = 0.0

    def stop(self) -> None:
        self._stopped = True

    def _should_stop(self) -> bool:
        if self._stopped:
            return True
        try:
            self._cancelled = jobs.cancel_requested(self.job_id)
        except Exception:  # noqa: BLE001 - unknown means "not cancelled"; asked again in a second
            return False
        return self._cancelled

    def _rclone(self, *args: str) -> rclone.Result:
        result = rclone.run([self.settings.rclone, *args], timeout=LIST_TIMEOUT,
                            should_stop=self._should_stop)
        if not result.ok:
            raise AlbumsError(result.reason(), auth=result.auth)
        return result

    def list_albums(self) -> list[str]:
        parent = self.settings.sync_parent
        names = parse_dirs(self._rclone("lsf", "--dirs-only", parent).out)
        if not names:
            raise AlbumsError(f"no albums under {parent}")
        return names

    def list_album(self, name: str) -> list[Entry]:
        path = remote_path(self.settings.sync_parent, name)
        return parse_entries(self._rclone("lsjson", "--metadata", "--files-only", path).out)

    def _say(self, message: str, progress: dict) -> None:
        if self.echo:
            self.echo(message)
        jobs.update_job(self.job_id, message=message, progress=progress)

    def run(self) -> str:
        """Run to the end; returns the job's final status."""
        progress = {"albums": 0, "done": 0, "current": ""}
        try:
            self._say("listing albums", progress)
            names = self.list_albums()
            listings: dict[str, list[Entry]] = {}
            for done, name in enumerate(names):
                progress = {"albums": len(names), "done": done, "current": name}
                self._say(f"reading {name} ({done + 1}/{len(names)})", progress)
                if len(name) > NAME_LIMIT:
                    log(f"albums: skipped an album with a {len(name)}-character name", error=True)
                    continue
                listings[name] = self.list_album(name)
            if not any(listings.values()) and self._has_rows():
                raise AlbumsError("iCloud listed no photos at all; nothing was changed")
            with SessionLocal() as db:
                outcome = apply(db, self.settings, listings)
        except rclone.Stopped:
            if self._cancelled:
                self.status = "cancelled"
                jobs.finish(self.job_id, "cancelled", message="cancelled", progress=progress)
            else:
                self.status = "failed"
                jobs.finish(self.job_id, "failed", message="agent stopped", error="agent stopped",
                            progress=progress)
            return self.status
        except (AlbumsError, rclone.RcloneError) as exc:
            error = auth_hint(self.settings) if getattr(exc, "auth", False) else str(exc)
            self.status = "failed"
            jobs.finish(self.job_id, "failed", message="album refresh failed", error=error,
                        progress=progress)
            log(f"albums: failed: {error}", error=True)
            return self.status
        self.status = "done"
        message = outcome.summary()
        jobs.finish(self.job_id, "done", message=message,
                    progress={"albums": len(listings), "done": len(listings), "current": ""})
        log(f"albums: {message} ({outcome.changed} photo(s) changed)")
        return self.status

    def _has_rows(self) -> bool:
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(Photo)
                             .where(Photo.root == self.settings.sync_root)) > 0
