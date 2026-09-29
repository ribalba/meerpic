"""Edited photos: ``IMG_1925-edited.heic`` in front of ``IMG_1925.HEIC``.

iCloud hands an edit over as a second file beside the original, named after
it with ``-edited``. Photos on the phone shows the edit and keeps the
original behind it; so does this: the original gets ``superseded_by`` = the
edit, and every listing leaves superseded rows out.

Which original an edit belongs to is decided by name, then by what the two
files say about themselves:

* the edit is ``STEM-edited.EXT`` (any case; iCloud adds ``_<suffix>`` after
  ``-edited`` when two edits collide, ``IMG_4201-edited_0B3A8D78-...heic``);
* candidates are the stills in the same folder named ``STEM.<ext>`` or
  ``STEM_<suffix>.<ext>``: when two assets share a name, "All Photos" gives
  both an iCloud id suffix, and the edit keeps the bare name;
* the one with the edit's ``content_id``, when both have one; else the one
  taken nearest in time. On the real library an edit's own identifier often
  differs from its original's, while the capture time is copied exactly.

Each edit claims at most one original and each original has at most one
edit. Only stills: an edited video (``IMG_0763-edited.mov``) stays a video of
its own, because the delete button takes an edit's originals with it, and a
wrong match among videos could be some other still's Live Photo motion.

``relink`` recomputes the groups it is given from what is in the table now,
so it is idempotent and repairs anything, like ``live.relink``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from core.models import Photo

_EDIT = re.compile(r"^(?P<stem>.+)-edited(?:_[^.]*)?\.[^.]+$", re.IGNORECASE)


def edit_stem(name: str) -> str | None:
    """The original's stem for an edit's file name, None for anything else."""
    m = _EDIT.match(name)
    return m.group("stem") if m else None


def _base(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def candidate_stems(name: str) -> set[str]:
    """Every stem (lower case) under which this file could be an original:
    ``IMG_0010_AQgX.HEIC`` -> {"img_0010_aqgx", "img_0010", "img"}."""
    base = _base(name).lower()
    out = {base}
    i = base.find("_")
    while i > 0:
        out.add(base[:i])
        i = base.find("_", i + 1)
    return out


def matches(stem: str, name: str) -> bool:
    """Whether ``name`` is ``STEM.<ext>`` or ``STEM_<suffix>.<ext>``."""
    base, stem = _base(name).lower(), stem.lower()
    return base == stem or base.startswith(stem + "_")


def _folder(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


@dataclass(frozen=True)
class _Row:
    id: int
    root: str
    rel_path: str
    name: str
    kind: str
    content_id: str
    taken_at: datetime | None
    superseded_by: int | None


_COLUMNS = (Photo.id, Photo.root, Photo.rel_path, Photo.name, Photo.kind, Photo.content_id,
            Photo.taken_at, Photo.superseded_by)


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def pick(edits: list[_Row], originals: list[_Row]) -> dict[int, int]:
    """original id -> edit id: the best pairs first, each side used once."""
    pairs = []
    for e in edits:
        for o in originals:
            same = bool(e.content_id and o.content_id and e.content_id == o.content_id)
            if e.taken_at is not None and o.taken_at is not None:
                apart = abs((e.taken_at - o.taken_at).total_seconds())
            else:
                apart = float("inf")
            pairs.append((not same, apart, e.id, o.id))
    pairs.sort()
    out: dict[int, int] = {}
    used: set[int] = set()
    for _, _, edit_id, original_id in pairs:
        if edit_id in used or original_id in out:
            continue
        out[original_id] = edit_id
        used.add(edit_id)
    return out


def relink(db: Session, rows: Iterable) -> int:
    """Recompute every edit group any of ``rows`` (anything with ``root``,
    ``rel_path`` and ``name``) can be part of. Returns how many rows changed.
    Does not commit."""
    wanted: dict[str, set[tuple[str, str]]] = {}          # root -> {(folder, stem)}
    for row in rows:
        stem = edit_stem(row.name)
        stems = {stem.lower()} if stem else candidate_stems(row.name)
        folder = _folder(row.rel_path)
        wanted.setdefault(row.root, set()).update((folder, s) for s in stems)
    changed = 0
    for root, keys in wanted.items():
        changed += _relink_root(db, root, keys)
    return changed


def _relink_root(db: Session, root: str, keys: set[tuple[str, str]]) -> int:
    # All edits of the root first: a few hundred rows in a real library, one
    # query on the name's trigram index. Only groups with an edit in them
    # need the (prefix) query for their candidates.
    edits_by_key: dict[tuple[str, str], list[_Row]] = {}
    for r in db.execute(select(*_COLUMNS).where(Photo.root == root, Photo.name.ilike("%-edited%"))):
        stem = edit_stem(r.name)
        if stem is None or r.kind != "photo":
            continue
        key = (_folder(r.rel_path), stem.lower())
        if key in keys:
            edits_by_key.setdefault(key, []).append(_Row(**r._mapping))

    changed = 0
    for (folder, stem), edits in edits_by_key.items():
        edit_ids = {e.id for e in edits}
        found = db.execute(select(*_COLUMNS).where(
            Photo.root == root,
            or_(func.lower(Photo.name).like(_like_prefix(stem)), Photo.superseded_by.in_(edit_ids)),
        )).all()
        rows = [_Row(**r._mapping) for r in found]
        originals = [
            r for r in rows
            if r.kind == "photo" and _folder(r.rel_path) == folder and matches(stem, r.name)
            and edit_stem(r.name) is None
            # Claimed by an edit of another group ("IMG-edited" for
            # "IMG_0010.HEIC"): that group's to keep or give up.
            and (r.superseded_by is None or r.superseded_by in edit_ids)
        ]
        chosen = pick(sorted(edits, key=lambda e: e.id), sorted(originals, key=lambda o: o.id))
        for r in rows:
            want = chosen.get(r.id)
            if r.superseded_by == want:
                continue
            if r.superseded_by is not None and r.superseded_by not in edit_ids and want is None:
                continue
            db.execute(update(Photo).where(Photo.id == r.id).values(superseded_by=want))
            changed += 1
    return changed
