"""Walking the library and bringing the ``photos`` table in line with it.

A walk is cheap: ``os.scandir`` hands back names and types from one syscall
per directory, and a ``stat`` per file is the rest. 25,000 files is a few
hundred milliseconds, which is what lets the agent do it every minute, and
every 30 seconds while a sync is downloading, instead of watching the folder
with inotify (which a bind mount into a container does not reliably carry).

A file is identified by where it is (root name + relative path) and changed
when its size or mtime is, which is what rclone changes when it replaces one.
New files get a provisional date from their mtime straight away, so the grid
sorts them correctly before exiftool has seen them.

The one rule that matters more than speed: **an empty or missing root deletes
nothing**. A disk that is not mounted, a network share that timed out, a
container started without its bind mount: each looks exactly like a library
whose every photo was deleted, and treating it that way would throw away an
index that took an hour to build. A directory that cannot be read keeps the
rows under it for the same reason.

A new file that is a photo deleted here, back from iCloud under another name
(or from an ``rclone copy`` run by hand), is not indexed: it goes back to the
trash (agent/delete.py, ``returned``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from core.media import classify, make_sig
from core.models import UNDATED, Photo
from core.timeutil import home_zone, utcnow

from . import dates, edits, live
from .delete import returned
from .log import log


@dataclass
class Walk:
    files: dict[str, tuple[int, int]] = field(default_factory=dict)   # rel -> (size, mtime_ns)
    unreadable: list[str] = field(default_factory=list)                # rel dirs, "" = the root
    skipped_names: int = 0                                             # not valid UTF-8


@dataclass
class ScanResult:
    new: int = 0
    changed: int = 0
    removed: int = 0
    kept: int = 0               # rows not deleted because their folder was not readable
    returned: int = 0           # deleted here, back from iCloud, into the trash again
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.new} new, {self.changed} changed, {self.removed} removed"

    def add(self, other: ScanResult) -> None:
        self.new += other.new
        self.changed += other.changed
        self.removed += other.removed
        self.kept += other.kept
        self.returned += other.returned
        self.warnings += other.warnings


def walk(root: Path) -> Walk:
    """Every media file under ``root``: relative path (``/``-separated) ->
    (size, mtime_ns). Hidden files and folders are skipped; symlinks to files
    are followed, symlinks to folders are not (a loop would never end)."""
    out = Walk()
    stack = [(str(root), "")]
    while stack:
        path, prefix = stack.pop()
        try:
            entries = os.scandir(path)
        except OSError:
            out.unreadable.append(prefix.rstrip("/"))
            continue
        with entries:
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, f"{prefix}{name}/"))
                        continue
                    if classify(name) is None or not entry.is_file(follow_symlinks=True):
                        continue
                    st = entry.stat(follow_symlinks=True)
                except OSError:     # vanished mid-walk (rclone renaming a .partial)
                    continue
                rel = prefix + name
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:
                    # A name that is not UTF-8 cannot be stored in Postgres
                    # text; the photo is skipped rather than the whole scan.
                    out.skipped_names += 1
                    continue
                out.files[rel] = (st.st_size, st.st_mtime_ns)
    return out


def provisional(mtime_ns: int, zone: ZoneInfo) -> dict:
    """The date columns a brand-new file starts with, from its mtime alone."""
    dated = dates.from_mtime(mtime_ns, zone)
    if dated is None:
        return {"taken_at": None, "taken_local": None, "tz_offset": None,
                "date_source": "none", "sort_at": UNDATED}
    return dated.columns()


def _under(rel: str, folders: list[str]) -> bool:
    return any(folder == "" or rel.startswith(folder + "/") for folder in folders)


def scan_root(db: Session, name: str, root: Path, zone: ZoneInfo,
              settings: Settings | None = None) -> ScanResult:
    result = ScanResult()
    known = {
        row.rel_path: row
        for row in db.execute(
            select(Photo.id, Photo.root, Photo.rel_path, Photo.name, Photo.size, Photo.mtime_ns,
                   Photo.content_id)
            .where(Photo.root == name)
        ).all()
    }

    if not root.is_dir():
        if known:
            msg = f"scan {name}: {root} is missing or not a folder; keeping its {len(known)} photo(s)"
            result.warnings.append(msg)
            result.kept = len(known)
        return result

    found = walk(root)
    if found.skipped_names:
        result.warnings.append(f"scan {name}: skipped {found.skipped_names} file(s) whose names are not UTF-8")
    if ("" in found.unreadable or not found.files) and known:
        why = "cannot be read" if "" in found.unreadable else "is empty"
        result.warnings.append(f"scan {name}: {root} {why}; keeping its {len(known)} photo(s)")
        result.kept = len(known)
        return result

    new = {rel: stat for rel, stat in found.files.items() if rel not in known}
    back = returned(db, settings or get_settings(), name, root, new) if new else set()
    result.returned = len(back)

    now = utcnow()
    fresh, changed = [], []
    for rel, (size, mtime_ns) in found.files.items():
        row = known.get(rel)
        if row is None:
            if rel in back:
                continue
            ext, kind, mime = classify(rel.rsplit("/", 1)[-1])
            fresh.append({
                "root": name, "rel_path": rel, "name": rel.rsplit("/", 1)[-1],
                "ext": ext, "kind": kind, "mime": mime, "size": size, "mtime_ns": mtime_ns,
                "sig": make_sig(name, rel, size, mtime_ns),
                **provisional(mtime_ns, zone),
                "created_at": now, "updated_at": now,
            })
        elif row.size != size or row.mtime_ns != mtime_ns:
            # A new sig makes every stage pending again (see core/models);
            # the old failures were about the old file.
            changed.append({
                "id": row.id, "size": size, "mtime_ns": mtime_ns,
                "sig": make_sig(name, rel, size, mtime_ns),
                "error_stage": "", "error": "", "fail_count": 0, "updated_at": now,
            })
    if fresh:
        db.execute(insert(Photo), fresh)
        result.new = len(fresh)
    if changed:
        db.execute(update(Photo), changed)      # bulk UPDATE by primary key
        result.changed = len(changed)

    gone = [row for rel, row in known.items() if rel not in found.files]
    if found.unreadable:
        keep = [row for row in gone if _under(row.rel_path, found.unreadable)]
        if keep:
            result.kept += len(keep)
            result.warnings.append(
                f"scan {name}: {len(found.unreadable)} folder(s) unreadable; keeping {len(keep)} photo(s) in them"
            )
            gone = [row for row in gone if not _under(row.rel_path, found.unreadable)]
    if gone:
        db.execute(delete(Photo).where(Photo.id.in_([row.id for row in gone])))
        # A deleted still frees its video, and a deleted video its still; a
        # deleted edit its original (the foreign key already did), which
        # another edit of the same name may now claim.
        live.relink(db, {row.content_id for row in gone})
        edits.relink(db, gone)
        result.removed = len(gone)
    return result


def scan(db: Session, settings: Settings) -> ScanResult:
    """Scan every configured root. Commits."""
    zone = home_zone(settings.timezone)
    total = ScanResult()
    for name, root in settings.roots.items():
        result = scan_root(db, name, root, zone, settings)
        db.commit()
        for warning in result.warnings:
            log(warning, error=True)
        total.add(result)
    return total
