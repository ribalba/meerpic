"""Rows to JSON.

Two rules shape this file, both meercal's:

* **Two kinds of time, never mixed up.** An *instant* (a job's start, the
  agent's heartbeat, when a photo was taken in UTC) leaves here as ISO text
  with a trailing ``Z``. A *wall clock* (``taken``: the time on the clock
  where the photo was taken) leaves with no suffix at all, and the browser
  prints it as it is. Converting it would move a photo taken at 23:30 in New
  York onto the next morning, which is the bug this is here to make
  impossible.
* **Every URL is decided here.** A derived file's URL carries the photo's
  ``sig`` (``?v=``), which changes when the file does, so the browser may keep
  it forever; the browser never builds one itself.
"""

from __future__ import annotations

from datetime import datetime

from core.config import get_settings
from core.media import original_path, preview_path
from core.models import Face, Job, Photo, PhotoText
from core.timeutil import home_zone

settings = get_settings()
TZ = home_zone(settings.timezone)

# The end of a job's log the status poll carries. The whole log is capped by
# the agent; forty lines is what fits in the sidebar's expanded card.
LOG_TAIL_LINES = 40

# What the export of a photo can hand over untouched: formats every mail
# client and every operating system opens. Everything else is converted.
EXPORT_AS_IS = {"jpg", "jpeg", "png"}


def iso_z(instant: datetime | None) -> str | None:
    """A naive UTC instant as ISO text with its ``Z``."""
    return instant.isoformat(timespec="seconds") + "Z" if instant else None


def wall(clock: datetime | None) -> str | None:
    """A naive wall clock as ISO text, deliberately without an offset."""
    return clock.isoformat(timespec="seconds") if clock else None


def stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def thumb_url(p: Photo) -> str | None:
    # Trusts the agent's word (thumb_sig) rather than the disk: this runs for
    # every tile of every page, and a stat() per tile is 150 system calls per
    # scroll for a file that is there in every case but a wiped cache.
    if p.thumb_sig and p.thumb_sig == p.sig:
        return f"/media/thumb/{p.id}?v={p.sig}"
    return None


def preview_ready(p: Photo | None) -> bool:
    """Whether a video's browser-playable copy is there to be played.

    Unlike the thumbnail this does look at the disk: it is asked for one photo
    at a time, and a <video> pointed at a missing file shows nothing at all,
    where a missing thumbnail at least shows its placeholder.
    """
    return bool(
        p is not None
        and p.kind == "video"
        and p.preview_sig
        and p.preview_sig == p.sig
        and preview_path(p.sig).is_file()
    )


def preview_url(p: Photo) -> str:
    return f"/media/preview/{p.id}?v={p.sig}"


def hover_preview_url(p: Photo) -> str | None:
    """A video tile's preview for playing on hover. Trusts ``preview_sig``,
    like thumb_url and for the same reason: it is asked for every tile."""
    if p.kind == "video" and p.preview_sig and p.preview_sig == p.sig:
        return preview_url(p)
    return None


def story_url(p: Photo) -> str | None:
    """A video's strip of frames for hover-scrubbing (core.media.story_path)."""
    if p.story_sig and p.story_sig == p.sig and p.story_frames > 0:
        return f"/media/story/{p.id}?v={p.sig}"
    return None


def nsfw_flagged(p: Photo) -> bool:
    """Above the threshold and not marked safe. Whether it looks just like a
    photo marked safe is a query (app/query.py, ``safe_cleared``), which the
    caller makes once per page and passes to card_json as ``cleared``."""
    return p.nsfw is not None and p.nsfw >= settings.nsfw_threshold and not p.nsfw_safe


def deletable() -> bool:
    """Whether Delete is offered at all ([library] delete). It moves files
    to the trash here and leaves iCloud as it is; see core/deletion.py."""
    return bool(settings.delete_enabled)


def _share_changes_things() -> bool:
    return bool(settings.share_strip_location or settings.share_max_edge > 0)


def export_plan(p: Photo) -> tuple[str, bool]:
    """(file name, direct) for what ``/media/export/{id}`` hands over.

    ``direct`` means "the original file, byte for byte", which is what lets
    the desktop shell drag the file off the disk instead of downloading it
    first. For a photo that is a JPEG or PNG when the share settings change
    nothing; everything else is converted to a JPEG. For a video it is an
    H.264 MP4 (every mail client plays it); otherwise the preview, which is
    one, when it exists, and the original when it does not.
    """
    tweaked = _share_changes_things()
    if p.kind == "video":
        if p.ext == "mp4" and p.video_codec == "h264" and not tweaked:
            return p.name, True
        # Trusts preview_sig for the same reason as thumb_url.
        if p.preview_sig and p.preview_sig == p.sig:
            return stem(p.name) + ".mp4", False
        return p.name, False
    if p.ext in EXPORT_AS_IS and not tweaked:
        return p.name, True
    return stem(p.name) + ".jpg", False


def _coord(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def card_json(p: Photo, score: float | None = None, edited: bool = False,
              cleared: bool = False) -> dict:
    """One tile of the grid. See SPEC "Card"; the UI is written against it.

    ``edited`` is whether some photo is this one's original, which the row
    cannot say by itself: the caller asks once per page (see
    routers/photos.edited_among) and passes the answer in. ``cleared`` is
    likewise whether it looks just like a photo marked safe, and so is not
    flagged although the classifier would flag it.
    """
    path = original_path(p.root, p.rel_path)
    export_name, export_direct = export_plan(p)
    out = {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "taken": wall(p.taken_local),
        "day": p.taken_local.date().isoformat() if p.taken_local else None,
        "date_source": p.date_source,
        "w": p.width,
        "h": p.height,
        "duration": p.duration,
        "live": p.live_video_id is not None,
        "screenshot": p.is_screenshot,
        "place": p.place,
        "city": p.city,
        "lat": _coord(p.lat),
        "lon": _coord(p.lon),
        "thumb": thumb_url(p),
        "path": str(path) if path else None,
        "export": f"/media/export/{p.id}",
        "export_name": export_name,
        "export_direct": export_direct,
        "favorite": p.favorite,
        "nsfw": round(p.nsfw, 4) if p.nsfw is not None else None,
        "nsfw_flag": nsfw_flagged(p) and not cleared,
        "nsfw_safe": p.nsfw_safe,
        "edited": edited,
        "preview": hover_preview_url(p),
        "story": story_url(p),
        "story_frames": p.story_frames,
    }
    if score is not None:
        out["score"] = round(float(score), 4)
    return out


def face_url(f: Face) -> str:
    return f"/media/face/{f.id}?v={f.sig}"


def face_json(f: Face) -> dict:
    """One face: its crop, and its box as fractions of the photo as shown."""
    return {
        "id": f.id,
        "url": face_url(f),
        "box": {"x": f.x, "y": f.y, "w": f.w, "h": f.h},
        "score": f.score,
    }


def faces_state(p: Photo) -> str:
    """Whether the faces stage has looked at this photo: ``done``, still
    ``pending``, or ``off`` when it never will (a video, the original behind
    an edit, face detection turned off). The same rows as its pending query
    (agent/faces.py)."""
    if not settings.faces_enabled or p.kind != "photo" or p.is_companion or p.superseded_by is not None:
        return "off"
    return "done" if p.faces_sig and p.faces_sig == p.sig else "pending"


def text_state(p: Photo) -> str:
    """Whether the text stage has read this file: ``done``, still
    ``pending``, or ``off`` when it never will. The same rows as its pending
    query (agent/ocr.py)."""
    if (not settings.ocr_enabled or p.is_companion or p.superseded_by is not None
            or (p.kind == "video" and not settings.ocr_videos)):
        return "off"
    return "done" if p.ocr_sig and p.ocr_sig == p.sig else "pending"


def detail_json(p: Photo, live: Photo | None = None, albums: list[str] | None = None,
                original_id: int | None = None, faces: list[Face] | None = None,
                text: PhotoText | None = None,
                safe_like: tuple[Photo, float] | None = None) -> dict:
    """Everything the viewer and its info panel show about one photo.

    ``live`` is the still's companion video, when it has one; ``albums`` the
    names of the albums it is in, in the order to show them; ``original_id``
    the photo this one is an edit of; ``faces`` the faces found in it,
    largest first; ``text`` what it says, if it says anything;
    ``safe_like`` the photo marked safe it looks most like, and how much
    (app/query.py, ``safe_lookalike``).
    """
    like, similarity = safe_like if safe_like is not None else (None, 0.0)
    cleared = like is not None and 0 < settings.nsfw_clear_above <= similarity
    out = card_json(p, edited=original_id is not None, cleared=cleared)
    thumb = out["thumb"]
    video = preview_url(p) if preview_ready(p) else None
    motion = preview_url(live) if preview_ready(live) else None
    out.update({
        "root": p.root,
        "rel_path": p.rel_path,
        "size": p.size,
        "mime": p.mime,
        "ext": p.ext,
        "taken_utc": iso_z(p.taken_at),
        "tz_offset": p.tz_offset,
        "camera": {"make": p.make, "model": p.model, "lens": p.lens},
        "exposure": {
            "iso": p.iso,
            "f_number": p.f_number,
            "exposure_time": p.exposure_time,
            "focal_length": p.focal_length,
        },
        "exif": p.exif or {},
        "location": None if p.lat is None or p.lon is None else {
            "lat": _coord(p.lat),
            "lon": _coord(p.lon),
            "altitude": p.altitude,
            "city": p.city,
            "region": p.region,
            "country": p.country,
            "country_code": p.country_code,
        },
        "video_codec": p.video_codec,
        "live_video_id": p.live_video_id,
        "urls": {
            # A video has no full-size still of its own; its poster frame is
            # the thumbnail.
            "display": thumb if p.kind == "video" else f"/media/display/{p.id}?v={p.sig}",
            "original": f"/media/original/{p.id}",
            "download": f"/media/original/{p.id}?download=1",
            "export": f"/media/export/{p.id}",
            "video": video,
            "live": motion,
        },
        "preview_ready": bool(video or motion),
        "error": p.error,
        "albums": list(albums or []),
        "added_at": iso_z(p.added_at),
        "original_id": original_id,
        "hidden": p.hidden,
        "icloud_deleted": p.icloud_deleted,
        "deletable": deletable(),
        # Not flagged because of it (cleared), or listed last in is:nsfw.
        "nsfw_like": None if like is None else {
            "id": like.id, "name": like.name, "thumb": thumb_url(like),
            "similarity": round(similarity, 3), "cleared": cleared,
        },
        "faces": [face_json(f) for f in faces or []],
        "faces_state": faces_state(p),
        "text_lines": list(text.lines) if text is not None else [],
        "text_state": text_state(p),
    })
    return out


def log_tail(log: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join((log or "").splitlines()[-lines:])


# Params the agent reads and the browser has no use for. A delete job carries
# its whole plan (see core.deletion), a few hundred bytes per file, and the
# status poll lists the job every two seconds while it runs and for a while
# after; the dialog that asked for it had the plan already.
AGENT_ONLY_PARAMS = ("plan",)


def job_json(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "params": {k: v for k, v in (job.params or {}).items() if k not in AGENT_ONLY_PARAMS},
        "progress": job.progress or {},
        "message": job.message,
        "error": job.error,
        # Not in the spec's list, and cheap: lets the sidebar say
        # "Cancelling..." between the click and the agent noticing.
        "cancel_requested": job.cancel_requested,
        "created_at": iso_z(job.created_at),
        "started_at": iso_z(job.started_at),
        "finished_at": iso_z(job.finished_at),
        "log_tail": log_tail(job.log),
    }
