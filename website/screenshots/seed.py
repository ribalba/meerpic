"""Wait for the demo agent to finish indexing, then add what iCloud would know.

Runs in the demo stack's agent container (it needs ``core``), against the demo
database only:

    make -C website demo-seed
    docker compose -f website/screenshots/docker-compose.yml exec agent \
        python /website/screenshots/seed.py

Favourites, albums and the WhatsApp album are not in any file: the real agent
reads them from iCloud after a sync (agent/albums.py), which the demo stack
has switched off. This writes the same rows that refresh would, from
demo_photos.py. Everything else in the database is the agent's own work on the
demo files, untouched.

It refuses to run against any database but ``meerpic_demo``: it rewrites the
album tables wholesale, as the real refresh does.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The app's own code, where the agent image keeps it.
sys.path.insert(0, os.environ.get("MEERPIC_APP_DIR", "/app"))
import demo_photos as demo
from sqlalchemy import delete, func, select, update

from core.config import get_settings
from core.database import SessionLocal, engine
from core.models import Album, Photo, PhotoAlbum, Setting
from core.timeutil import utcnow

TIMEOUT = 45 * 60
POLL = 5


def guard() -> None:
    name = engine.url.database or ""
    if name != "meerpic_demo":
        sys.exit(f"refusing to seed {name!r}: this rewrites the album tables and only ever "
                 "runs against the demo stack's meerpic_demo database")


def expected(rows: list[demo.Resolved]) -> set[tuple[str, str, int, int]]:
    """Every demo file as the scanner will record it: root, path, size, mtime.

    Size and time as well as the name, so a library rebuilt in place is not
    taken for indexed while the database still describes the old files.
    """
    roots = get_settings().roots
    out = set()
    for r in rows:
        for n in r.names:
            st = (roots[r.item.root] / n).stat()
            out.add((r.item.root, n, st.st_size, st.st_mtime_ns))
    return out


def wait_for_agent(rows: list[demo.Resolved]) -> None:
    """Until every demo file is in the database and no stage has work left."""
    want = expected(rows)
    started = time.monotonic()
    last = ""
    settled = 0
    while True:
        with SessionLocal() as db:
            have = set(db.execute(select(Photo.root, Photo.rel_path, Photo.size, Photo.mtime_ns)).all())
            status = db.get(Setting, "agent.status")
            pending = dict((status.value or {}).get("pending") or {}) if status else {}
        missing = len(want - have)
        failed = pending.pop("failed", 0)
        left = {k: v for k, v in pending.items() if v}
        line = f"indexed {len(want) - missing}/{len(want)}" + (
            f", left: {', '.join(f'{k} {v}' for k, v in sorted(left.items()))}" if left else "")
        if failed:
            line += f", failed {failed}"
        if line != last:
            print(f"  {line}", flush=True)
            last = line
        if not missing and pending and not left:
            settled += 1
            # Three times, fifteen seconds: the agent refreshes its counts
            # every ten, so a zero can be a count from before the last scan.
            if settled >= 3:
                return
        else:
            settled = 0
        if time.monotonic() - started > TIMEOUT:
            sys.exit(f"gave up after {TIMEOUT // 60} minutes: {line}")
        time.sleep(POLL)


def seed(rows: list[demo.Resolved]) -> None:
    now = utcnow()
    with SessionLocal() as db:
        ids = {(p.root, p.rel_path): p.id for p in db.execute(select(Photo.root, Photo.rel_path, Photo.id)).all()}

        def members(r: demo.Resolved) -> list[int]:
            # The still and its edit; a Live Photo's video is hidden anyway.
            return [ids[(r.item.root, n)] for n in (r.rel_path, r.edit) if n and (r.item.root, n) in ids]

        favourites = [pid for r in rows if r.item.favorite for pid in members(r)]
        db.execute(update(Photo).values(favorite=False))
        if favourites:
            db.execute(update(Photo).where(Photo.id.in_(favourites)).values(favorite=True))

        # WhatsApp saves: the moment they arrived is iCloud's "added" time.
        for r in rows:
            if r.item.kind == "whatsapp" and (r.item.root, r.rel_path) in ids:
                db.execute(update(Photo).where(Photo.id == ids[(r.item.root, r.rel_path)])
                           .values(added_at=r.utc.replace(tzinfo=None)))

        albums: dict[str, list[int]] = {}
        for r in rows:
            for name in r.item.albums:
                albums.setdefault(name, []).extend(members(r))
            if r.item.kind == "whatsapp":
                albums.setdefault(demo.WHATSAPP, []).extend(members(r))

        db.execute(delete(PhotoAlbum))
        db.execute(delete(Album))
        for name, photo_ids in sorted(albums.items()):
            album = Album(name=name, kind="user", count=len(photo_ids), remote_count=len(photo_ids),
                          synced_at=now)
            db.add(album)
            db.flush()
            db.add_all(PhotoAlbum(album_id=album.id, photo_id=pid) for pid in sorted(set(photo_ids)))
        db.commit()

        total = db.scalar(select(func.count()).select_from(Photo))
        broken = db.execute(select(Photo.rel_path, Photo.error_stage, Photo.error)
                            .where(Photo.error != "")).all()
    print(f"seeded: {len(favourites)} favourites, {len(albums)} albums "
          f"({', '.join(f'{k} {len(v)}' for k, v in sorted(albums.items()))}), {total} files in the database")
    for path, stage, error in broken:
        print(f"  error in {stage} for {path}: {error[:200]}")


def main() -> int:
    guard()
    rows = demo.resolve()
    print("waiting for the demo agent to index the library...", flush=True)
    wait_for_agent(rows)
    seed(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
