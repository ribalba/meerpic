"""What a Delete deletes, decided once, in one place.

Delete deletes here, not in iCloud: rclone's iCloud Photos backend is
read-only (every write, deletefile included, is "not implemented"), so a
photo from iCloud stays there and on the phone. Here its files move to
``.meerpic-trash/<day>/`` in the library folder, and their names are
remembered (core.models.DeletedFile) so that no later sync downloads them
again.

The server shows the plan in the confirmation dialog, move for move; the
agent runs the plan it was handed in the job, and nothing else. Both call
this module, so the list a person agreed to and the list that runs cannot
drift apart: the dialog does not describe the deletion, it *is* it.

One photo can be several files, and deleting only one of them leaves the
rest behind as an orphan that the next sync then shows again:

* a Live Photo's still and its two-second video;
* an edit (``IMG_1925-edited.heic``) and the original it supersedes, which
  in iCloud are one asset.

So a photo's group is the photo, its Live companion, the originals it
supersedes, and, when it is itself an original, its edit and that edit's
companion.

Only files that came from iCloud are kept out of later syncs: the sync
root, top level, with sync on. Anything else (another library root, a
subfolder) is not something a sync brings, and only moves to the trash.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .media import original_path, trash_path
from .models import Photo
from .timeutil import home_zone, utc_to_wall, utcnow

# One Delete at a time is a review, not a purge: past this it is a mistake
# with a selection.
MAX_PHOTOS = 500


@dataclass
class Step:
    """One file of one group."""

    photo_id: int
    name: str
    local: str                      # absolute path of the file here
    trash: str                      # where it moves
    # The name every later sync excludes, the name in iCloud's "All Photos";
    # None for a file that did not come from iCloud.
    excluded: str | None


@dataclass
class Group:
    """What deleting one chosen photo takes with it."""

    photo_id: int
    steps: list[Step] = field(default_factory=list)


@dataclass
class Plan:
    groups: list[Group]
    missing: list[int]              # ids asked for that do not exist (or are already going)

    @property
    def photo_ids(self) -> list[int]:
        return [s.photo_id for g in self.groups for s in g.steps]

    @property
    def moves(self) -> list[str]:
        return [f"{s.local} -> {s.trash}" for g in self.groups for s in g.steps]

    @property
    def excluded(self) -> list[str]:
        return [s.excluded for g in self.groups for s in g.steps if s.excluded]

    def to_json(self) -> dict:
        return {
            "groups": [{"photo_id": g.photo_id, "steps": [asdict(s) for s in g.steps]} for g in self.groups],
            "moves": self.moves,
            "excluded": self.excluded,
            "missing": self.missing,
        }

    @classmethod
    def from_json(cls, data: dict) -> Plan:
        """The plan a job carries. A step with keys this version does not
        know is refused (TypeError), not trimmed: a plan from before Delete
        was local held rclone commands, and was agreed to as a delete in
        iCloud, which is not what this one would do with it."""
        known = {f.name for f in fields(Step)}
        groups = []
        for g in data.get("groups") or []:
            steps = []
            for s in g.get("steps") or []:
                if set(s) != known:
                    raise TypeError(f"a step with keys {sorted(s)} is not one of this version's")
                steps.append(Step(**s))
            groups.append(Group(photo_id=int(g["photo_id"]), steps=steps))
        return cls(groups=groups, missing=list(data.get("missing") or []))


def in_icloud(photo: Photo, settings: Settings) -> bool:
    """Whether this file came from iCloud: where ``rclone copy`` puts every
    file, the top of the sync root. Such a file would come back with the
    next sync unless its name is excluded."""
    return bool(settings.sync_enabled and photo.root == settings.sync_root and "/" not in photo.rel_path)


def _group_rows(db: Session, photo: Photo) -> list[Photo]:
    """The photo and every file that must go with it, the photo first."""
    rows: dict[int, Photo] = {photo.id: photo}

    def add(p: Photo | None) -> None:
        if p is not None and p.id not in rows:
            rows[p.id] = p

    stills = [photo]
    if photo.superseded_by:
        edit = db.get(Photo, photo.superseded_by)
        add(edit)
        if edit is not None:
            stills.append(edit)
    originals = db.scalars(select(Photo).where(
        Photo.superseded_by.in_([s.id for s in stills])
    )).all()
    for original in originals:
        add(original)
        stills.append(original)
    for still in stills:
        if still.live_video_id:
            add(db.get(Photo, still.live_video_id))
    return list(rows.values())


def plan(db: Session, ids: list[int], settings: Settings, *, today: date | None = None) -> Plan:
    """The plan for deleting these photos. Reads, never writes.

    Photos already being deleted (``trashed_at`` set) count as missing, so a
    double click cannot plan the same file twice.
    """
    # The trash folder is named for the day in the library's own zone, the
    # one a person looking for yesterday's deletions will think in.
    day = (today or utc_to_wall(utcnow(), home_zone(settings.timezone)).date()).isoformat()
    seen: set[int] = set()
    groups: list[Group] = []
    missing: list[int] = []
    wanted = list(dict.fromkeys(int(i) for i in ids))[:MAX_PHOTOS]
    found = {p.id: p for p in db.scalars(select(Photo).where(Photo.id.in_(wanted)))}
    for photo_id in wanted:
        photo = found.get(photo_id)
        if photo is None or photo.trashed_at is not None:
            missing.append(photo_id)
            continue
        if photo.id in seen:
            continue            # already part of an earlier group
        group = Group(photo_id=photo.id)
        for row in _group_rows(db, photo):
            if row.id in seen or row.trashed_at is not None:
                continue
            seen.add(row.id)
            local = original_path(row.root, row.rel_path)
            trash = trash_path(row.root)
            group.steps.append(Step(
                photo_id=row.id,
                name=row.name,
                local=str(local) if local else "",
                trash=str(trash / day / row.rel_path) if trash else "",
                # `rclone copy` keeps the remote's names, so the local name is
                # the name in "All Photos", disambiguating suffix and all.
                excluded=row.name if in_icloud(row, settings) else None,
            ))
        if group.steps:
            groups.append(group)
    return Plan(groups=groups, missing=missing)
