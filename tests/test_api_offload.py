"""Old videos kept as a small copy, and their originals offloaded to iCloud.

A video past ``offload.after`` keeps a small copy in its library folder
(core.media.lowres_path). Once iCloud is seen to hold the same file, its
original is deleted here and downloaded again when somebody asks for it
(core/offloading.py). The agent makes the copies, deletes and downloads. What
is tested here is the server's part. That is what a tile and the viewer are
told, which bytes go out for a video whose original is gone, the request to
get it back, the search for such videos and what the sidebar counts as still
to do. The small copies are arbitrary bytes, because nothing here decodes them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from server_helpers import (
    RED,
    add_file,
    image_bytes,
    library,
    make_photo,
    needs_db,
    running_app,
    session,
    truncate,
    wall,
    write_preview,
)

pytestmark = needs_db


@pytest.fixture(scope="module")
def client():
    with running_app() as c:
        yield c


@pytest.fixture
def db():
    with session() as s:
        yield s


@pytest.fixture
def settings(monkeypatch):
    """The live settings, with videos offloaded once they are a year old."""
    from core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "offload_after", 365)
    return s


IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"
IN_ICLOUD_ONLY = "The original is in iCloud only; get it from the viewer first"
ORIGINAL_BYTES = b"\x00\x00\x00\x14ftypqt  " + bytes(500)
SMALL_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
OLD = wall(2020, 5, 1, 18)                                          # years past offload.after
RECENT = datetime.combine(datetime.now(UTC).date() - timedelta(days=30), time(12))
GONE_AT = datetime.combine(date(2026, 1, 2), time(3, 4, 5))          # naive UTC, as stored


def write_lowres(photo, data: bytes = SMALL_BYTES):
    """The agent's small copy of ``photo``, where the agent would put it."""
    from core.media import lowres_path

    path = lowres_path(photo.root, photo.name, photo.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    photo.lowres_sig = photo.sig
    return path


@pytest.fixture
def lib(client, settings):
    """A fresh library per test, with a video in each state an old one goes through.

    Values are (id, sig). The originals of the offloaded ones are never
    written, and each test's rows have new sigs, so no small copy of an
    earlier test is mistaken for one of these.
    """
    from core.database import SessionLocal
    from core.media import OFFLOAD_DIR, lowres_name

    truncate()
    with SessionLocal() as s:
        p = {}
        # A month old. Nothing about it changes.
        p["normal"] = add_file(s, "OFF_0001.MOV", ORIGINAL_BYTES, taken=RECENT, video_codec="hevc")
        # Old enough. Its small copy is made and its original is still here.
        p["kept"] = add_file(s, "OFF_0002.MOV", ORIGINAL_BYTES, taken=OLD, video_codec="hevc")
        write_lowres(p["kept"])
        # Offloaded. The small copy is all there is of it here.
        p["offloaded"] = make_photo(s, "OFF_0003.MOV", taken=OLD, video_codec="hevc", offloaded_at=GONE_AT)
        write_lowres(p["offloaded"])
        # Offloaded, and its small copy deleted by somebody since.
        p["bare"] = make_photo(s, "OFF_0004.MOV", taken=OLD, video_codec="hevc", offloaded_at=GONE_AT)
        p["bare"].lowres_sig = p["bare"].sig
        # Downloaded again, and the row not unmarked yet.
        p["back"] = add_file(s, "OFF_0005.MOV", ORIGINAL_BYTES, taken=OLD, video_codec="hevc",
                             offloaded_at=GONE_AT)
        write_lowres(p["back"])
        # Stepped aside (agent/albums.py). iCloud has another file under its name now.
        aside = make_photo(s, "OFF_0006.MOV", taken=OLD, video_codec="hevc", offloaded_at=GONE_AT)
        aside.rel_path = f"{OFFLOAD_DIR}/{lowres_name(aside.name, aside.sig)}"
        write_lowres(aside)
        p["aside"] = aside
        p["still"] = add_file(s, "OFF_0007.JPG", image_bytes(RED))
        s.commit()
        ids = {k: (v.id, v.sig) for k, v in p.items()}
    yield ids
    truncate()


@pytest.fixture
def empty(client, settings):
    truncate()
    yield
    truncate()


def card_of(client, pid: int, q: str = "") -> dict:
    """The tile the grid gets for one photo, taken from the listing itself."""
    r = client.get("/api/photos", params={"q": q, "limit": 500})
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["items"] if c["id"] == pid)


def detail_of(client, pid: int) -> dict:
    r = client.get(f"/api/photos/{pid}")
    assert r.status_code == 200, r.text
    return r.json()


def lowres_url(pid: int, sig: str) -> str:
    return f"/media/lowres/{pid}?v={sig}"


def jobs_of(db, kind: str) -> list:
    from core.models import Job

    db.expire_all()
    return db.query(Job).filter(Job.kind == kind).order_by(Job.id).all()


# --- what a tile and the viewer are told --------------------------------------


def test_a_recent_video_is_not_offloaded(client, lib):
    pid, _ = lib["normal"]
    path = str(library() / "OFF_0001.MOV")
    card = card_of(client, pid)
    assert card["offloaded"] is False
    assert card["path"] == path
    assert card["preview"] is None
    assert (card["export_name"], card["export_direct"]) == ("OFF_0001.MOV", False)

    d = detail_of(client, pid)
    assert d["offloaded"] is False
    assert d["path"] == path
    assert (d["fetchable"], d["offloaded_at"], d["lowres_size"]) == (False, None, None)
    assert d["urls"]["lowres"] is None
    assert d["urls"]["video"] is None


def test_an_old_video_plays_its_small_copy_while_its_original_is_here(client, lib):
    pid, sig = lib["kept"]
    path = str(library() / "OFF_0002.MOV")
    card = card_of(client, pid)
    assert card["offloaded"] is False
    assert card["path"] == path
    # No 720p copy is made for it, so hovering plays the small one.
    assert card["preview"] == lowres_url(pid, sig)
    # Its export is still the original.
    assert (card["export_name"], card["export_direct"]) == ("OFF_0002.MOV", False)

    d = detail_of(client, pid)
    assert d["offloaded"] is False
    assert d["path"] == path
    assert (d["fetchable"], d["offloaded_at"]) == (False, None)
    assert d["lowres_size"] == len(SMALL_BYTES)
    assert d["urls"]["lowres"] == lowres_url(pid, sig)
    assert d["urls"]["video"] is None
    assert d["preview_ready"] is False


def test_a_720p_preview_comes_before_the_small_copy(client, lib, db):
    from core.models import Photo

    pid, sig = lib["kept"]
    write_preview(db.get(Photo, pid))
    db.commit()
    assert card_of(client, pid)["preview"] == f"/media/preview/{pid}?v={sig}"
    d = detail_of(client, pid)
    assert d["urls"]["video"] == f"/media/preview/{pid}?v={sig}"
    assert d["urls"]["lowres"] == lowres_url(pid, sig)


def test_a_small_copy_of_an_older_version_is_not_offered(client, lib, db):
    from core.models import Photo

    pid, _ = lib["kept"]
    db.get(Photo, pid).lowres_sig = "0000000000000000"
    db.commit()
    assert card_of(client, pid)["preview"] is None
    d = detail_of(client, pid)
    assert (d["urls"]["lowres"], d["lowres_size"]) == (None, None)


def test_an_offloaded_video_is_its_small_copy(client, lib):
    pid, sig = lib["offloaded"]
    card = card_of(client, pid)
    assert card["offloaded"] is True
    assert card["path"] is None                      # nothing to drag off this disk
    assert card["preview"] == lowres_url(pid, sig)
    assert (card["export_name"], card["export_direct"]) == ("OFF_0003.mp4", False)
    assert card["thumb"] == f"/media/thumb/{pid}?v={sig}"

    d = detail_of(client, pid)
    assert d["offloaded"] is True
    assert d["path"] is None
    assert d["fetchable"] is True
    assert d["offloaded_at"] == "2026-01-02T03:04:05Z"
    assert d["lowres_size"] == len(SMALL_BYTES)
    assert d["urls"]["lowres"] == lowres_url(pid, sig)
    assert d["urls"]["video"] is None
    assert d["preview_ready"] is False


def test_an_offloaded_video_without_its_small_copy_can_still_be_fetched(client, lib):
    pid, _ = lib["bare"]
    d = detail_of(client, pid)
    assert d["offloaded"] is True
    assert d["fetchable"] is True
    # The viewer looks at the disk, and the small copy is not there.
    assert (d["urls"]["lowres"], d["lowres_size"]) == (None, None)


def test_an_original_that_is_back_is_not_offloaded_to_the_viewer(client, lib):
    pid, sig = lib["back"]
    # The tile trusts the row, which still says offloaded.
    assert card_of(client, pid)["offloaded"] is True
    # The viewer checks the disk, where the original is.
    d = detail_of(client, pid)
    assert d["offloaded"] is False
    assert (d["fetchable"], d["offloaded_at"]) == (False, None)
    assert d["urls"]["lowres"] == lowres_url(pid, sig)
    assert d["lowres_size"] == len(SMALL_BYTES)


def test_the_viewer_has_the_path_of_an_original_that_is_back(client, lib):
    pid, _ = lib["back"]
    d = detail_of(client, pid)
    assert d["offloaded"] is False
    assert d["path"] == str(library() / "OFF_0005.MOV")


def test_a_video_that_stepped_aside_is_offloaded_and_cannot_be_fetched(client, lib):
    pid, sig = lib["aside"]
    d = detail_of(client, pid)
    assert d["offloaded"] is True
    assert d["path"] is None
    assert d["fetchable"] is False                  # iCloud's file of that name is another one now
    assert d["urls"]["lowres"] == lowres_url(pid, sig)


# --- the bytes ----------------------------------------------------------------


def test_the_small_copy_is_served_with_ranges(client, lib):
    for key in ("kept", "offloaded"):
        pid, sig = lib[key]
        r = client.get(lowres_url(pid, sig))
        assert r.status_code == 200
        assert r.content == SMALL_BYTES
        assert r.headers["content-type"] == "video/mp4"
        assert r.headers["cache-control"] == IMMUTABLE
        assert r.headers["accept-ranges"] == "bytes"
        part = client.get(lowres_url(pid, sig), headers={"range": "bytes=10-19"})
        assert part.status_code == 206
        assert part.content == SMALL_BYTES[10:20]
        assert part.headers["content-range"] == f"bytes 10-19/{len(SMALL_BYTES)}"


def test_a_small_copy_under_a_missing_or_stale_sig_is_not_pinned(client, lib):
    pid, _ = lib["offloaded"]
    assert client.get(f"/media/lowres/{pid}").headers["cache-control"] == REVALIDATE
    assert client.get(f"/media/lowres/{pid}?v=0000000000000000").headers["cache-control"] == REVALIDATE


def test_no_small_copy_is_a_404(client, lib, db):
    from core.models import Photo

    for key in ("normal", "bare", "still"):
        r = client.get(f"/media/lowres/{lib[key][0]}")
        assert r.status_code == 404, key
        assert r.json()["detail"] == "No small copy"
    assert client.get("/media/lowres/999999").status_code == 404

    # The copy on disk is of an older version of the file.
    pid, _ = lib["kept"]
    db.get(Photo, pid).lowres_sig = "0000000000000000"
    db.commit()
    assert client.get(f"/media/lowres/{pid}").status_code == 404


def test_an_offloaded_video_exports_its_small_copy(client, lib):
    pid, _ = lib["offloaded"]
    r = client.get(f"/media/export/{pid}")
    assert r.status_code == 200
    assert r.content == SMALL_BYTES
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"] == 'attachment; filename="OFF_0003.mp4"'
    assert r.headers["cache-control"] == REVALIDATE


def test_a_video_that_stepped_aside_exports_its_small_copy(client, lib):
    r = client.get(f"/media/export/{lib['aside'][0]}")
    assert r.status_code == 200
    assert r.content == SMALL_BYTES
    assert r.headers["content-disposition"] == 'attachment; filename="OFF_0006.mp4"'


def test_an_offloaded_video_without_its_small_copy_has_nothing_to_export(client, lib):
    r = client.get(f"/media/export/{lib['bare'][0]}")
    assert r.status_code == 404
    assert r.json()["detail"] == IN_ICLOUD_ONLY


def test_an_old_video_exports_its_original_while_it_is_here(client, lib):
    for key, name in (("kept", "OFF_0002.MOV"), ("back", "OFF_0005.MOV")):
        r = client.get(f"/media/export/{lib[key][0]}")
        assert r.status_code == 200, key
        assert r.content == ORIGINAL_BYTES
        assert r.headers["content-disposition"] == f'attachment; filename="{name}"'


def test_the_original_of_an_offloaded_video_is_in_icloud_only(client, lib, db):
    for key in ("offloaded", "bare"):
        pid, _ = lib[key]
        for url in (f"/media/original/{pid}", f"/media/original/{pid}?download=1"):
            r = client.get(url)
            assert r.status_code == 404, url
            assert r.json()["detail"] == IN_ICLOUD_ONLY

    # A missing file that was never offloaded says so differently.
    lost = make_photo(db, "OFF_0010.MOV", taken=OLD)
    db.commit()
    r = client.get(f"/media/original/{lost.id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "The original file is not there"


def test_an_original_that_is_back_is_served(client, lib):
    r = client.get(f"/media/original/{lib['back'][0]}")
    assert r.status_code == 200
    assert r.content == ORIGINAL_BYTES


# --- getting the original back ------------------------------------------------


def test_a_video_whose_original_is_here_needs_no_fetch(client, lib, db):
    for key in ("normal", "kept", "back"):
        r = client.post(f"/api/photos/{lib[key][0]}/fetch")
        assert r.status_code == 200, key
        assert r.json() == {"ready": True}
    assert jobs_of(db, "fetch") == []


def test_a_still_is_never_fetched(client, lib):
    r = client.post(f"/api/photos/{lib['still'][0]}/fetch")
    assert r.status_code == 400
    assert r.json()["detail"] == "Only videos are offloaded"
    assert client.post("/api/photos/999999/fetch").status_code == 404


def test_a_video_icloud_cannot_hand_back_is_a_409(client, lib, db, settings, monkeypatch):
    # Stepped aside, its name is another file's in iCloud now.
    r = client.post(f"/api/photos/{lib['aside'][0]}/fetch")
    assert r.status_code == 409
    assert "only its small copy is left" in r.json()["detail"]
    # With sync off, nothing is copied from iCloud at all.
    monkeypatch.setattr(settings, "sync_enabled", False)
    assert client.post(f"/api/photos/{lib['offloaded'][0]}/fetch").status_code == 409
    assert detail_of(client, lib["offloaded"][0])["fetchable"] is False
    assert jobs_of(db, "fetch") == []


def test_a_fetch_is_queued_once(client, lib, db):
    pid, _ = lib["offloaded"]
    first = client.post(f"/api/photos/{pid}/fetch").json()
    again = client.post(f"/api/photos/{pid}/fetch").json()
    assert first["ready"] is False
    assert first["status"] == "queued"
    assert again == first
    [job] = jobs_of(db, "fetch")
    assert (job.id, job.status, job.params) == (first["job_id"], "queued", {"photo_id": pid})

    # While it runs, asking again is the same download.
    job.status = "running"
    db.commit()
    assert client.post(f"/api/photos/{pid}/fetch").json() == {
        "ready": False, "job_id": first["job_id"], "status": "running",
    }

    # Once that one is finished, asking again is a new request.
    job.status = "failed"
    db.commit()
    third = client.post(f"/api/photos/{pid}/fetch").json()
    assert third["job_id"] != first["job_id"]
    assert len(jobs_of(db, "fetch")) == 2

    # A fetch for another video is a download of its own.
    other = client.post(f"/api/photos/{lib['bare'][0]}/fetch").json()
    assert other["job_id"] not in (first["job_id"], third["job_id"])


# --- asking for a preview -----------------------------------------------------


def test_an_offloaded_video_has_no_preview_to_make(client, lib, db):
    for key in ("offloaded", "bare"):
        r = client.post(f"/api/photos/{lib[key][0]}/preview")
        assert r.status_code == 409, key
        assert r.json()["detail"] == "The original is in iCloud only: get it first"
    assert jobs_of(db, "preview") == []


def test_an_offloaded_video_with_a_preview_left_says_where_it_is(client, lib, db):
    from core.models import Photo

    pid, sig = lib["offloaded"]
    write_preview(db.get(Photo, pid))
    db.commit()
    assert client.post(f"/api/photos/{pid}/preview").json() == {
        "ready": True, "url": f"/media/preview/{pid}?v={sig}",
    }


def test_a_preview_of_an_old_video_whose_original_is_here_is_queued(client, lib, db):
    for key in ("kept", "back"):
        answer = client.post(f"/api/photos/{lib[key][0]}/preview").json()
        assert answer["ready"] is False, key
    assert [j.params for j in jobs_of(db, "preview")] == [
        {"photo_id": lib["kept"][0]}, {"photo_id": lib["back"][0]},
    ]


# --- finding them -------------------------------------------------------------


def test_is_offloaded_finds_the_videos_whose_original_is_in_icloud(client, lib):
    r = client.get("/api/photos", params={"q": "is:offloaded", "limit": 500})
    assert r.status_code == 200, r.text
    assert {c["id"] for c in r.json()["items"]} == {
        lib[k][0] for k in ("offloaded", "bare", "back", "aside")
    }
    # They are still in the grid like any other video.
    everything = {c["id"] for c in client.get("/api/photos", params={"limit": 500}).json()["items"]}
    assert everything == {v[0] for v in lib.values()}


# --- what is still to do ------------------------------------------------------


def index(client) -> dict:
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    return r.json()["index"]


def test_old_videos_are_counted_as_small_copies_to_make_and_not_as_previews(
        client, empty, db, settings, monkeypatch):
    from core.timeutil import utcnow

    make_photo(db, "IDX_01.MOV", taken=OLD)                               # its small copy to make
    copied = make_photo(db, "IDX_02.MOV", taken=OLD)                      # made already
    copied.lowres_sig = copied.sig
    gone = make_photo(db, "IDX_03.MOV", taken=OLD, offloaded_at=GONE_AT, meta_sig="")
    gone.lowres_sig = gone.sig
    make_photo(db, "IDX_04.MOV", taken=RECENT)                            # too young, so a preview
    make_photo(db, "IDX_05.MOV", taken=OLD, is_companion=True)            # a Live Photo's motion
    make_photo(db, "IDX_06.MOV", rel_path="imports/IDX_06.MOV", taken=OLD)  # not from iCloud
    make_photo(db, "IDX_07.MOV", taken=OLD, icloud_deleted=True)          # leaving iCloud
    make_photo(db, "IDX_08.MOV", taken=OLD, fetched_at=utcnow() - timedelta(days=1))   # fetched lately
    make_photo(db, "IDX_09.MOV", taken=OLD, fetched_at=utcnow() - timedelta(days=60))  # fetched long ago
    make_photo(db, "IDX_10.MOV", taken=None)                              # no date, no age
    make_photo(db, "IDX_11.MOV", taken=OLD, fail_count=3, error="bad file", error_stage="meta")
    make_photo(db, "IDX_12.HEIC", meta_sig="")                            # metadata to read
    db.commit()

    idx = index(client)
    assert idx["lowres"] == 2                # 01 and 09
    assert idx["offloaded"] == 1             # 03
    assert idx["previews"] == 6              # 04, 05, 06, 07, 08 and 10
    assert idx["meta"] == 1                  # 12, and not the offloaded 03
    assert idx["failed"] == 1


def test_with_offloading_off_old_videos_want_their_preview_again(client, empty, db, settings, monkeypatch):
    make_photo(db, "IDX_30.MOV", taken=OLD)
    copied = make_photo(db, "IDX_31.MOV", taken=OLD)
    copied.lowres_sig = copied.sig
    make_photo(db, "IDX_32.MOV", taken=RECENT)
    db.commit()
    assert (index(client)["lowres"], index(client)["previews"]) == (1, 1)

    monkeypatch.setattr(settings, "offload_after", 0)
    idx = index(client)
    assert (idx["lowres"], idx["offloaded"], idx["previews"]) == (0, 0, 3)


@pytest.mark.parametrize("case", ["offload turned off", "in Recently Deleted"])
def test_an_offloaded_video_is_never_a_preview_to_make(client, empty, db, settings, monkeypatch, case):
    gone = make_photo(db, "IDX_20.MOV", taken=OLD, offloaded_at=GONE_AT,
                      icloud_deleted=case == "in Recently Deleted")
    gone.lowres_sig = gone.sig
    db.commit()
    if case == "offload turned off":
        monkeypatch.setattr(settings, "offload_after", 0)
    assert index(client)["previews"] == 0
