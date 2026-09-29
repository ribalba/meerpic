"""Live Photos: tying a still to its two-second video.

An iPhone writes a Live Photo as two files, a HEIC and a MOV, and stamps both
with one ``ContentIdentifier``. That shared value is the only link: names can
differ (iCloud appends a suffix to one and not the other when two assets
collide), and so can dates by a second.

The rule: at least one still and at least one video with the same
identifier. Every still of the group gets ``live_video_id`` (an edited photo
and its original share the identifier, and both are the same moment), and
the video becomes a companion and disappears from listings. When a group has
several videos, only the first is the motion (an original's before an
edit's); the others stay visible, because hiding a video by mistake loses it
where showing one twice only repeats it. A video alone pairs nothing.

``relink`` recomputes whole groups from what is in the table now, so it is
idempotent and repairs anything: a still that was deleted (its video comes
back), a file whose identifier changed, a pairing made by an older version.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.models import Photo

from .edits import edit_stem


def relink(db: Session, content_ids: Iterable[str], photo_ids: Iterable[int] = ()) -> int:
    """Recompute the groups for ``content_ids``; unlink ``photo_ids`` that no
    longer have an identifier. Returns how many rows changed. Does not commit.
    """
    cids = sorted({c for c in content_ids if c})
    changed = 0

    loose = list(set(photo_ids))
    if loose:
        # Lost its identifier (a replaced file, a reindex): whatever it was
        # linked to before is no longer backed by anything.
        changed += db.execute(
            update(Photo)
            .where(Photo.id.in_(loose), Photo.content_id == "")
            .where((Photo.is_companion.is_(True)) | (Photo.live_video_id.is_not(None)))
            .values(is_companion=False, live_video_id=None)
        ).rowcount or 0

    if not cids:
        return changed

    rows = db.execute(
        select(Photo.id, Photo.name, Photo.kind, Photo.content_id, Photo.is_companion,
               Photo.live_video_id)
        .where(Photo.content_id.in_(cids))
        .order_by(Photo.id)
    ).all()
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row.content_id].append(row)

    for members in groups.values():
        stills = [r for r in members if r.kind == "photo"]
        videos = sorted((r for r in members if r.kind == "video"),
                        key=lambda r: (edit_stem(r.name) is not None, r.id))
        motion = videos[0].id if stills and videos else None
        for r in stills:
            want = motion
            if r.live_video_id != want:
                db.execute(update(Photo).where(Photo.id == r.id).values(live_video_id=want))
                changed += 1
        for r in videos:
            want = r.id == motion
            if r.is_companion != want or r.live_video_id is not None:
                db.execute(update(Photo).where(Photo.id == r.id)
                           .values(is_companion=want, live_video_id=None))
                changed += 1
    return changed
