"""Delete: into the trash here, and never back with a sync.

rclone can read iCloud Photos but not change them: its Photos backend is
read-only, and ``rclone deletefile`` there answers "optional feature not
implemented". So Delete is local. The photo stays in iCloud and on the phone;
here its files move into ``.meerpic-trash/<day>/`` inside the library folder
(see core/media.py), emptied after thirty days, and the rows go, and the
derived files with them.

A file that came from iCloud would come straight back with the next
``rclone copy``, so it leaves a note (core.models.DeletedFile): its name,
which every sync excludes (agent/sync.py), and its content, by which the scan
knows it again should it arrive under another name after all (``returned``).
Moving a file back out of the trash by hand is how a delete is undone: the
scan finds it in the library again with its note's trash copy gone, drops the
note and indexes the file like any new one.

What runs is decided before anything runs. The server plans the deletion
with core/deletion.py, shows the person every move, and stores the plan they
confirmed in the job; this runs that plan and nothing else. It recomputes no
group and no name. It does check each step before running it, and refuses
one that would move a file other than its photo's, or move it anywhere but
that root's trash, or exclude any name but the file's own: whatever wrote the
job row, the agent only ever moves photos into the trash.

A group (a photo, its Live motion, an edit and its original) moves as one. If
one of its files cannot be moved, the ones already moved go back and the rows
stay. The server marked the photos ``trashed_at`` when Delete was pressed, so
they left the grid at once; a group that fails gets it cleared and comes
back, and the job says which and why.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.config import Settings
from core.database import SessionLocal
from core.deletion import Group, Plan, Step, in_icloud
from core.media import (
    display_path,
    original_path,
    preview_path,
    story_path,
    thumb_path,
    trash_path,
)
from core.models import DeletedFile, Photo
from core.timeutil import home_zone, utc_to_wall, utcnow

from . import edits, jobs, live
from .log import log

KEEP_DAYS = 30
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DeleteError(RuntimeError):
    pass


def free_name(path: Path) -> Path:
    """``path``, or ``name (2).ext`` and so on: the trash never overwrites."""
    if not path.exists() and not path.is_symlink():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def today(settings: Settings) -> date:
    """The day in the library's own zone, as core/deletion.py names folders."""
    return utc_to_wall(utcnow(), home_zone(settings.timezone)).date()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rename(src: Path, dest: Path) -> None:
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:     # the trash is on another disk after all
            raise
        shutil.move(str(src), str(dest))


def purge_trash(settings: Settings, keep_days: int = KEEP_DAYS) -> int:
    """Empty day folders older than ``keep_days`` from every root's trash.
    Returns how many folders went."""
    cutoff = today(settings) - timedelta(days=keep_days)
    gone: list[str] = []
    for name in settings.roots:
        trash = trash_path(name)
        if trash is None or not trash.is_dir():
            continue
        for entry in os.scandir(trash):
            if not _DAY.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                day = date.fromisoformat(entry.name)
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                gone.append(entry.path)
    if gone:
        # Their notes stay, so the photos stay out of every sync; only the
        # trash copy is gone, which must not read as "taken back out".
        with SessionLocal() as db:
            for folder in gone:
                db.execute(
                    update(DeletedFile)
                    .where(DeletedFile.trash.startswith(folder.rstrip("/") + "/", autoescape=True))
                    .values(trash=None)
                )
            db.commit()
    return len(gone)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(folder.resolve(strict=False))
    except ValueError:
        return False
    return True


def check_step(settings: Settings, step: Step, row) -> None:
    """Refuse a step that is not what a plan from core/deletion.py can hold."""
    here = original_path(row.root, row.rel_path)
    trash = trash_path(row.root)
    if here is None or trash is None:
        raise DeleteError(f"library root {row.root!r} is not configured")
    if step.local != str(here):
        raise DeleteError(f"refused: the plan's file {step.local!r} is not this photo's")
    if not step.trash or not _inside(Path(step.trash), trash):
        raise DeleteError(f"refused: {step.trash!r} is not in {trash}")
    # Excluded when, and only when, it came from iCloud, and then by its own
    # name: anything else would keep some other photo out of every sync, or
    # let this one come back with the next.
    if (step.excluded is not None) != in_icloud(row, settings) or (
        step.excluded is not None and step.excluded != row.name
    ):
        raise DeleteError(f"refused: {step.name} does not match its plan (settings changed?)")


def move(step: Step) -> Path | None:
    """Move one file to its step's trash path. None when it was gone already."""
    src = Path(step.local)
    if not src.exists() and not src.is_symlink():
        return None
    dest = free_name(Path(step.trash))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _rename(src, dest)
    return dest


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def returned(db: Session, settings: Settings, name: str, root: Path,
             files: dict[str, tuple[int, int]]) -> set[str]:
    """Of the files a scan found and has no row for, the deleted ones that
    are back. Returns their relative paths, which the scan must not index.

    Every sync excludes the deleted names, but a name can change under a
    photo in iCloud (see core.models.DeletedFile), and an ``rclone copy`` run
    by hand excludes nothing. A new file at the top of the sync root with the
    size and content of a deleted one is that photo again: it goes to the
    trash too, and its note takes the new name, so the next sync skips it.
    Unless the note's own trash copy is gone: then the file was moved back
    out of the trash by hand, which undoes the delete, and the note goes.

    Changes notes in ``db``; the scan commits them.
    """
    if name != settings.sync_root or not files:
        return set()
    by_size: dict[int, list[DeletedFile]] = defaultdict(list)
    for note in db.scalars(select(DeletedFile).where(
        DeletedFile.root == name, DeletedFile.sha256.is_not(None)
    )):
        by_size[note.size].append(note)
    if not by_size:
        return set()
    trash = trash_path(name)
    skip: set[str] = set()
    for rel, (size, _mtime_ns) in files.items():
        if "/" in rel or not by_size.get(size):
            continue
        path = root / rel
        try:
            digest = sha256_of(path)
        except OSError:
            continue
        note = next((n for n in by_size[size] if n.sha256 == digest), None)
        if note is None:
            continue
        if note.trash is not None and not os.path.lexists(note.trash):
            log(f"trash: {rel} is back from the trash; showing it again")
            by_size[size].remove(note)
            db.delete(note)
            continue
        dest = free_name(trash / today(settings).isoformat() / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rename(path, dest)
        except OSError as exc:
            log(f"trash: {rel} was deleted here and is back, and cannot be moved: {exc}", error=True)
            skip.add(rel)       # still deleted: not indexed, tried again next scan
            continue
        note.name, note.trash = rel, str(dest)
        skip.add(rel)
        log(f"trash: {rel} was deleted here and came back from iCloud; moved it to the trash again")
    return skip


@dataclass(frozen=True)
class _Named:
    """The three attributes ``edits.relink`` reads, for a row already deleted."""

    root: str
    rel_path: str
    name: str


class DeleteRun:
    """One delete job: the plan in ``params.plan``, one group at a time."""

    def __init__(self, job_id: int, settings: Settings, params: dict | None = None, *,
                 echo: Callable[[str], None] | None = None):
        self.job_id = job_id
        self.settings = settings
        self.params = params or {}
        self.echo = echo
        self.status = "running"
        self._stopped = False
        self._cancelled = False
        self.lines: list[str] = []

    def stop(self) -> None:
        self._stopped = True

    def _should_stop(self) -> bool:
        if self._stopped:
            return True
        try:
            self._cancelled = jobs.cancel_requested(self.job_id)
        except Exception:  # noqa: BLE001 - unknown means "not cancelled"
            return False
        return self._cancelled

    def _say(self, line: str) -> None:
        self.lines.append(line)
        if self.echo:
            self.echo(line)
        jobs.append_log(self.job_id, [line])

    # --- one group ---------------------------------------------------------------

    def _put_back(self, moved: list[tuple[Path, Path]]) -> None:
        for src, dest in reversed(moved):
            if os.path.lexists(src):
                log(f"delete: cannot put {dest} back: {src} exists again", error=True)
                continue
            try:
                _rename(dest, src)
            except OSError as exc:
                log(f"delete: cannot put {dest} back to {src}: {exc}", error=True)

    def delete_group(self, db: Session, group: Group) -> int:
        """Run one group: the notes, the moves, the rows. Returns how many
        files moved. Raises DeleteError (or OSError, SQLAlchemyError) with
        every file back where it was and the rows as they were."""
        ids = [step.photo_id for step in group.steps]
        rows = {r.id: r for r in db.scalars(select(Photo).where(Photo.id.in_(ids)))}
        # A row gone since the plan was made was deleted some other way (a
        # scan that found the file missing): nothing left to do for it.
        steps = [step for step in group.steps if step.photo_id in rows]
        for step in steps:
            check_step(self.settings, step, rows[step.photo_id])

        # The note is of the file as it is now, read before anything moves.
        notes: list[tuple[Step, int, int, str | None]] = []
        for step in steps:
            if step.excluded is None:
                continue
            path = Path(step.local)
            try:
                stat = path.stat()
            except FileNotFoundError:
                row = rows[step.photo_id]
                notes.append((step, row.size, row.mtime_ns, None))
                continue
            notes.append((step, stat.st_size, stat.st_mtime_ns, sha256_of(path)))

        moved: list[tuple[Path, Path]] = []
        doomed = [rows[step.photo_id] for step in steps]
        sigs = [row.sig for row in doomed]
        try:
            for step in steps:
                dest = move(step)
                if dest is not None:
                    moved.append((Path(step.local), dest))
                self._say(f"  moved {step.local} -> {dest}" if dest else f"  {step.local}: no file here")
            went = {str(src): dest for src, dest in moved}
            for step, size, mtime_ns, digest in notes:
                dest = went.get(step.local)
                db.add(DeletedFile(
                    root=rows[step.photo_id].root, name=step.excluded, size=size, mtime_ns=mtime_ns,
                    sha256=digest, trash=str(dest) if dest else None,
                ))
            cids = {row.content_id for row in doomed}
            names = [_Named(row.root, row.rel_path, row.name) for row in doomed]
            db.execute(sql_delete(Photo).where(Photo.id.in_([row.id for row in doomed])))
            db.expire_all()
            # Whatever was paired with them is on its own now.
            live.relink(db, cids)
            edits.relink(db, names)
            db.commit()
        except BaseException:
            db.rollback()
            self._put_back(moved)
            raise
        for sig in sigs:
            for path in (thumb_path(sig), preview_path(sig), story_path(sig), display_path(sig)):
                _unlink(path)
        return len(moved)

    def restore(self, groups: list[Group]) -> None:
        """Back into the grid: the delete of these did not happen."""
        self._unmark([step.photo_id for group in groups for step in group.steps])

    @staticmethod
    def _unmark(ids: list[int]) -> None:
        if not ids:
            return
        with SessionLocal() as db:
            db.execute(update(Photo).where(Photo.id.in_(ids)).values(trashed_at=None))
            db.commit()

    # --- the job -------------------------------------------------------------------

    def run(self) -> str:
        try:
            plan = Plan.from_json(self.params.get("plan") or {})
        except (KeyError, TypeError, ValueError) as exc:
            # Its photos were marked when Delete was pressed; they come back.
            self._unmark([i for i in self.params.get("photo_ids") or [] if isinstance(i, int)])
            return self._end("failed", "bad delete plan", f"The job's plan cannot be read: {exc}",
                             {"done": 0, "total": 0, "failed": 0})
        groups = plan.groups
        total = len(groups)
        if not self.settings.delete_enabled:
            self.restore(groups)
            return self._end("failed", "delete is off",
                             "Delete is turned off ([library] delete in meerpic.toml).",
                             {"done": 0, "total": total, "failed": total})
        if not groups:
            return self._end("done", "Nothing to delete", None, {"done": 0, "total": 0, "failed": 0})

        done = failed = 0
        errors: list[str] = []
        for i, group in enumerate(groups):
            progress = {"done": done, "total": total, "failed": failed}
            if self._should_stop():
                self.restore(groups[i:])
                if self._cancelled:
                    return self._end("cancelled", "cancelled", None, progress)
                return self._end("failed", "agent stopped", "agent stopped", progress)
            name = group.steps[0].name if group.steps else str(group.photo_id)
            jobs.update_job(self.job_id, progress=progress, message=f"deleting {name} ({i + 1}/{total})")
            with SessionLocal() as db:
                try:
                    self.delete_group(db, group)
                except (DeleteError, OSError, SQLAlchemyError) as exc:
                    reason = str(exc) if isinstance(exc, DeleteError) else f"{type(exc).__name__}: {exc}"
                    failed += 1
                    errors.append(f"{name}: {reason}")
                    self._say(f"failed {name}: {reason}")
                    log(f"delete: {name}: {reason}", error=True)
                    self.restore([group])
                    continue
            done += 1
            extra = len(group.steps) - 1
            self._say(f"moved {name} to the trash" + (f" (+{extra} file(s) with it)" if extra else ""))
        progress = {"done": done, "total": total, "failed": failed}
        if failed:
            return self._end("failed", f"Moved {done} of {total} to the trash", "\n".join(errors), progress)
        return self._end("done", f"Moved {done} to the trash", None, progress)

    def _end(self, status: str, message: str, error: str | None, progress: dict) -> str:
        self.status = status
        jobs.finish(self.job_id, status, message=message, error=error, progress=progress)
        log(f"delete: {message}" + (f": {error.splitlines()[0]}" if error else ""),
            error=status == "failed")
        return status
