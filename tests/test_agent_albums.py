"""Albums, favourites, Hidden and Recently Deleted from iCloud's listing.

The fixtures have the shape ``rclone lsjson --metadata --files-only`` gives
for rclone 1.74's iCloud Photos backend (recorded on a real album, names and
numbers made up): ModTime with the local offset, ``added-time`` in UTC, the
flags as strings, ``width``/``height`` on stills only. A fake rclone serves
them; one test runs the real rclone over a local folder tree shaped like the
remote.
"""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import json
import os
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from agent_helpers import (
    ROOT,
    add_photo,
    local_remote,
    needs_db,
    needs_rclone,
    use_db,
    use_library,
)
from sqlalchemy import select

from agent import albums, jobs
from core.config import get_settings
from core.models import Album, DeletedFile, Job, Photo, PhotoAlbum
from core.timeutil import utcnow

MTIME = 1_700_300_126        # 2023-11-18T09:35:26Z


def entry(name, size, *, mtime=MTIME, favorite=False, hidden=False, added="2023-11-18T09:35:26Z",
          still=True):
    stamp = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
    meta = {"added-time": added, "favorite": "true" if favorite else "false",
            "hidden": "true" if hidden else "false"}
    if still:
        meta |= {"width": "4032", "height": "3024"}
    return {"Path": name, "Name": name, "Size": size, "MimeType": "image/heif", "ModTime": stamp,
            "IsDir": False, "Metadata": meta}


# --- parsing --------------------------------------------------------------------------


def test_parse_dirs():
    text = "All Photos/\nFavorites/\nUrlaub Polen mit Kindern/\nHühner/\n\n"
    assert albums.parse_dirs(text) == ["All Photos", "Favorites", "Urlaub Polen mit Kindern", "Hühner"]


def test_parse_entries():
    text = json.dumps([
        entry("IMG_1.HEIC", 3163442, favorite=True),
        entry("IMG_1.MOV", 4007983, still=False),
        {"Path": "sub", "Name": "sub", "IsDir": True},
    ])
    a, b = albums.parse_entries(text)
    assert (a.name, a.size, a.mtime, a.favorite, a.hidden) == ("IMG_1.HEIC", 3163442, MTIME, True, False)
    assert a.added == datetime(2023, 11, 18, 9, 35, 26)
    assert b.name == "IMG_1.MOV" and not b.favorite
    assert albums.parse_entries("[]") == []
    with pytest.raises(albums.AlbumsError):
        albums.parse_entries("[\n")


@pytest.mark.parametrize("text, epoch", [
    ("2023-11-18T10:35:26+01:00", MTIME),
    ("2023-11-18T09:35:26Z", MTIME),
    ("2023-11-18T10:35:26.826783706+01:00", MTIME),
    ("", None),
    ("yesterday", None),
])
def test_epoch_seconds(text, epoch):
    assert albums.epoch_seconds(text) == epoch


def test_remote_paths_and_the_main_album(lib):
    assert albums.remote_path("iclouddrive:PrimarySync", "WhatsApp") == "iclouddrive:PrimarySync/WhatsApp"
    assert albums.remote_path("photos:", "WhatsApp") == "photos:WhatsApp"
    assert albums.main_album(get_settings()) == "All Photos"


# --- matching ---------------------------------------------------------------------------


class R:
    def __init__(self, id, name, size, mtime=MTIME):
        self.id, self.name, self.size, self.mtime_ns = id, name, size, mtime * 10**9 + 627_000_000


def test_matching_by_name_then_by_size_time_and_stem():
    m = albums.Matcher([
        R(1, "IMG_0886_AUJZT／／pqw.HEIC", 500),
        R(2, "IMG_0886_AafwI.HEIC", 700),
        R(3, "IMG_0886.HEIC", 900),          # a third asset that kept the bare name
        R(4, "c3e65ce8.jpg", 100),
    ])
    E = albums.Entry
    assert m.match(E("c3e65ce8.jpg", 100, MTIME)) == 4
    # The album's bare name, the size of one of the suffixed ones.
    assert m.match(E("IMG_0886.HEIC", 700, MTIME)) == 2
    assert m.match(E("IMG_0886.HEIC", 500, MTIME + 1)) == 1        # a second either way
    assert m.match(E("IMG_0886.HEIC", 900, MTIME)) == 3
    # Same name, a size nobody has: still the file of that name.
    assert m.match(E("IMG_0886.HEIC", 123, MTIME)) == 3
    assert m.match(E("IMG_0886.JPG", 700, MTIME)) is None           # another extension
    assert m.match(E("IMG_0886.HEIC", 700, MTIME + 60)) == 3        # time too far: by name
    assert m.match(E("IMG_9999.HEIC", 700, MTIME)) is None


# --- writing it down -----------------------------------------------------------------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


def photo(db, name, size, **cols):
    return add_photo(db, name, size=size, mtime_ns=MTIME * 10**9 + 627_000_000, **cols)


def listings():
    E = albums.Entry
    added = datetime(2023, 11, 18, 9, 35, 26)
    return {
        "All Photos": [E("IMG_1.HEIC", 10, MTIME, added=added), E("IMG_1.MOV", 11, MTIME, added=added),
                       E("IMG_2.HEIC", 20, MTIME, favorite=True, added=datetime(2024, 1, 1)),
                       E("wa.jpg", 30, MTIME, added=datetime(2025, 5, 5)),
                       E("not-here.jpg", 99, MTIME)],
        "Favorites": [E("IMG_1.HEIC", 10, MTIME)],
        "Hidden": [E("secret.jpg", 40, MTIME, hidden=True, added=datetime(2022, 2, 2))],
        "Recently Deleted": [E("gone.jpg", 50, MTIME)],
        "WhatsApp": [E("wa.jpg", 30, MTIME), E("IMG_2.HEIC", 20, MTIME)],
    }


@needs_db
def test_apply_writes_flags_albums_and_membership(db, lib):
    p1, m1 = photo(db, "IMG_1.HEIC", 10), photo(db, "IMG_1.MOV", 11)
    p2, wa = photo(db, "IMG_2.HEIC", 20), photo(db, "wa.jpg", 30)
    secret, gone = photo(db, "secret.jpg", 40), photo(db, "gone.jpg", 50)
    sub = photo(db, "wa.jpg", 30, rel_path="imports/wa.jpg")          # not what rclone copied
    stale = add_photo(db, "Old.jpg", size=1)
    album = Album(name="Deleted Album", kind="user", count=1, remote_count=1)
    db.add(album)
    db.commit()
    db.add(PhotoAlbum(album_id=album.id, photo_id=stale.id))
    stale.favorite = True
    db.commit()

    out = albums.apply(db, get_settings(), listings(), now=datetime(2026, 9, 29, 12, 0))
    assert out.listed["All Photos"] == 5 and out.matched["All Photos"] == 4
    assert out.summary() == "5 albums, 2 in WhatsApp"
    db.expire_all()
    got = {p.id: p for p in db.scalars(select(Photo))}
    assert got[p1.id].favorite and got[p2.id].favorite                 # album / metadata
    assert not got[wa.id].favorite and not got[stale.id].favorite      # iCloud is the truth
    assert got[secret.id].hidden and not got[p1.id].hidden
    assert got[gone.id].icloud_deleted and not got[wa.id].icloud_deleted
    assert got[p1.id].added_at == datetime(2023, 11, 18, 9, 35, 26)
    assert got[wa.id].added_at == datetime(2025, 5, 5)
    assert got[secret.id].added_at == datetime(2022, 2, 2)             # from its own album
    assert got[sub.id].added_at is None and got[m1.id].added_at is not None

    names = {a.name: a for a in db.scalars(select(Album))}
    assert set(names) == {"All Photos", "Favorites", "Hidden", "Recently Deleted", "WhatsApp"}
    assert names["WhatsApp"].kind == "user" and names["Favorites"].kind == "smart"
    assert (names["WhatsApp"].count, names["WhatsApp"].remote_count) == (2, 2)
    assert names["All Photos"].synced_at == datetime(2026, 9, 29, 12, 0)
    members = {(a, p) for a, p in db.execute(select(PhotoAlbum.album_id, PhotoAlbum.photo_id))}
    assert (names["WhatsApp"].id, wa.id) in members and (names["WhatsApp"].id, sub.id) not in members
    assert len(members) == 4 + 1 + 1 + 1 + 2

    # The same listing again changes no photo.
    assert albums.apply(db, get_settings(), listings()).changed == 0


@needs_db
def test_a_deleted_name_another_photo_holds_now_is_given_up(db, lib):
    """A photo deleted here (and later on the phone) left its plain name free,
    and a new IMG_0001 took it. No sync may leave the new one out."""
    E = albums.Entry
    ns = MTIME * 10**9
    db.add_all([
        # Still there, the same photo: stays excluded.
        DeletedFile(root=ROOT, name="same.jpg", size=10, mtime_ns=ns, sha256="0" * 64),
        # The same photo, a different size in the listing (see Matcher.match): stays.
        DeletedFile(root=ROOT, name="resized.jpg", size=10, mtime_ns=ns, sha256="1" * 64),
        # Another photo under the name: given up.
        DeletedFile(root=ROOT, name="IMG_0001.HEIC", size=10, mtime_ns=ns, sha256="2" * 64),
        # Not listed at all (renamed, or gone from iCloud): stays.
        DeletedFile(root=ROOT, name="unlisted.jpg", size=10, mtime_ns=ns, sha256="3" * 64),
    ])
    db.commit()
    out = albums.apply(db, get_settings(), {"All Photos": [
        E("same.jpg", 10, MTIME), E("resized.jpg", 12, MTIME + 1), E("IMG_0001.HEIC", 99, MTIME + 86400),
    ]})
    assert out.released == 1
    db.expire_all()
    left = {n.sha256[0]: n.name for n in db.scalars(select(DeletedFile))}
    assert left == {"0": "same.jpg", "1": "resized.jpg", "2": None, "3": "unlisted.jpg"}
    # Another album's listing is not "All Photos": nothing is given up on it.
    db.execute(DeletedFile.__table__.update().values(name="IMG_0001.HEIC").where(DeletedFile.sha256 == "2" * 64))
    db.commit()
    assert albums.apply(db, get_settings(), {"WhatsApp": [E("IMG_0001.HEIC", 99, MTIME + 86400)]}).released == 0


@needs_db
def test_startup_due(db, lib, monkeypatch):
    s = get_settings()
    assert albums.startup_due(db, s)                                    # never refreshed
    db.add(Album(name="All Photos", kind="smart", synced_at=utcnow() - timedelta(hours=2)))
    db.commit()
    assert not albums.startup_due(db, s)
    assert albums.startup_due(db, s, now=utcnow() + timedelta(days=1))
    monkeypatch.setattr(s, "sync_albums", False)
    assert not albums.startup_due(db, s, now=utcnow() + timedelta(days=1))


# --- the job, against a fake rclone -----------------------------------------------------


def fake_rclone(tmp_path, dirs, contents, *, fail_on=None, fail_text="", hang_on=None):
    """An rclone that answers ``lsf --dirs-only`` and ``lsjson`` from
    fixtures, and records every call."""
    data = tmp_path / "fixtures.json"
    data.write_text(json.dumps({"dirs": dirs, "contents": contents}))
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "rclone-fake"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys, time
        args = sys.argv[1:]
        with open({str(calls)!r}, "a") as fh:
            fh.write(json.dumps(args) + "\\n")
        fx = json.load(open({str(data)!r}))
        target = args[-1]
        album = target.rsplit("/", 1)[-1]
        if album == {hang_on!r}:
            time.sleep(60)
        if album == {fail_on!r} or (args[0] == "lsf" and {fail_on!r} == "*"):
            print("2026/09/29 12:01:49 NOTICE: iclouddrive photos: parallel cold listing", file=sys.stderr)
            print({fail_text!r}, file=sys.stderr)
            sys.exit(1)
        if args[0] == "lsf":
            print("".join(d + "/\\n" for d in fx["dirs"]), end="")
        elif args[0] == "lsjson":
            print("[")
            print(",\\n".join(json.dumps(e) for e in fx["contents"].get(album, [])))
            print("]")
        """))
    script.chmod(0o755)
    return script, calls


def read_calls(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def job(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


FIXTURE_DIRS = ["All Photos", "Favorites", "WhatsApp"]
FIXTURE = {
    "All Photos": [entry("IMG_1.HEIC", 10), entry("wa.jpg", 30, added="2025-05-05T08:00:00Z")],
    "Favorites": [entry("IMG_1.HEIC", 10)],
    "WhatsApp": [entry("wa.jpg", 30), entry("elsewhere.jpg", 31)],
}


@needs_db
def test_the_job_lists_every_album_and_writes_once(db, lib, tmp_path, monkeypatch):
    script, calls = fake_rclone(tmp_path, FIXTURE_DIRS, FIXTURE)
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    p1, wa = photo(db, "IMG_1.HEIC", 10), photo(db, "wa.jpg", 30)
    j = jobs.create("albums", running=True)
    assert albums.AlbumsRun(j.id, get_settings()).run() == "done"
    row = job(db, j.id)
    assert (row.status, row.message) == ("done", "3 albums, 1 in WhatsApp")
    assert row.progress == {"albums": 3, "done": 3, "current": ""}
    assert read_calls(calls) == [
        ["lsf", "--dirs-only", "iclouddrive:PrimarySync"],
        ["lsjson", "--metadata", "--files-only", "iclouddrive:PrimarySync/All Photos"],
        ["lsjson", "--metadata", "--files-only", "iclouddrive:PrimarySync/Favorites"],
        ["lsjson", "--metadata", "--files-only", "iclouddrive:PrimarySync/WhatsApp"],
    ]
    db.expire_all()
    assert db.get(Photo, p1.id).favorite and db.get(Photo, wa.id).added_at == datetime(2025, 5, 5, 8)
    assert db.scalar(select(Album).where(Album.name == "WhatsApp")).remote_count == 2


@needs_db
def test_an_expired_session_fails_the_job_and_changes_nothing(db, lib, tmp_path, monkeypatch):
    expired = ('2026/09/29 12:01:50 CRITICAL: Failed to create file system for "iclouddrive:PrimarySync": '
               'missing icloud trust token: try refreshing it with "rclone config reconnect iclouddrive:"')
    script, _ = fake_rclone(tmp_path, FIXTURE_DIRS, FIXTURE, fail_on="WhatsApp", fail_text=expired)
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    p1 = photo(db, "IMG_1.HEIC", 10)
    j = jobs.create("albums", running=True)
    assert albums.AlbumsRun(j.id, get_settings()).run() == "failed"
    row = job(db, j.id)
    assert row.error == "The iCloud session has expired. In a terminal run: rclone config reconnect iclouddrive:"
    assert row.progress["current"] == "WhatsApp"
    db.expire_all()
    assert not db.get(Photo, p1.id).favorite                   # Favorites was read, not written
    assert db.scalar(select(Album)) is None


@needs_db
def test_other_failures_keep_rclones_words(db, lib, tmp_path, monkeypatch):
    script, _ = fake_rclone(tmp_path, FIXTURE_DIRS, FIXTURE, fail_on="*",
                            fail_text="2026/09/29 12:08:59 ERROR : error listing: directory not found")
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    j = jobs.create("albums", running=True)
    albums.AlbumsRun(j.id, get_settings()).run()
    assert job(db, j.id).error == "error listing: directory not found"


@needs_db
def test_no_albums_or_no_photos_listed_changes_nothing(db, lib, tmp_path, monkeypatch):
    script, _ = fake_rclone(tmp_path, [], {})
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    j = jobs.create("albums", running=True)
    assert albums.AlbumsRun(j.id, get_settings()).run() == "failed"
    assert "no albums under iclouddrive:PrimarySync" in job(db, j.id).error
    # Albums, but empty ones, over a library with photos in it: a glitch,
    # not a reason to clear every favourite.
    photo(db, "IMG_1.HEIC", 10, favorite=True)
    script, _ = fake_rclone(tmp_path, ["All Photos"], {"All Photos": []})
    j = jobs.create("albums", running=True)
    assert albums.AlbumsRun(j.id, get_settings()).run() == "failed"
    assert "listed no photos" in job(db, j.id).error
    db.expire_all()
    assert db.scalar(select(Photo.favorite))


@needs_db
def test_cancel_ends_a_listing(db, lib, tmp_path, monkeypatch):
    script, _ = fake_rclone(tmp_path, FIXTURE_DIRS, FIXTURE, hang_on="All Photos")
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    j = jobs.create("albums", running=True)
    row = job(db, j.id)
    row.cancel_requested = True
    db.commit()
    started = utcnow()
    assert albums.AlbumsRun(j.id, get_settings()).run() == "cancelled"
    assert (utcnow() - started).total_seconds() < 20
    assert job(db, j.id).status == "cancelled"


@needs_db
@needs_rclone
def test_real_rclone_over_a_local_tree(db, lib, tmp_path, monkeypatch):
    """The argv the job builds, run by the real rclone: a local folder per
    album, no iCloud metadata (so matching by name, flags from membership)."""
    main = local_remote(tmp_path, monkeypatch)
    favorites = main.parent / "Favorites"
    favorites.mkdir()
    for folder, name, data in ((main, "a.jpg", b"aaaa"), (main, "b.jpg", b"bb"), (favorites, "a.jpg", b"aaaa")):
        (folder / name).write_bytes(data)
        os.utime(folder / name, (MTIME, MTIME))
    a, b = photo(db, "a.jpg", 4), photo(db, "b.jpg", 2)
    j = jobs.create("albums", running=True)
    assert albums.AlbumsRun(j.id, get_settings()).run() == "done", job(db, j.id).error
    db.expire_all()
    assert db.get(Photo, a.id).favorite and not db.get(Photo, b.id).favorite
    counts = {x.name: (x.count, x.remote_count) for x in db.scalars(select(Album))}
    assert counts == {"All Photos": (2, 2), "Favorites": (1, 1)}
    assert ROOT == get_settings().sync_root
