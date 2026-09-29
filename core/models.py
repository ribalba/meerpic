"""SQLAlchemy ORM models for meerpic.

Design notes
------------
* A **photo** is one file in the library: a still or a video. Its identity is
  ``(root, rel_path)``, the library folder's name from meerpic.toml and the path
  inside it, so the folder can move without the database noticing anything but
  a changed line in the config.
* **The file is the truth.** Nothing here is ever written back to a photo: every
  column is derived from the file, and ``make reindex`` could rebuild all of
  them. That is also why there is no "edit the date" feature: the date is
  whatever the camera said.
* **Processing is staged**, and each stage remembers which version of the file
  it last processed by storing that version's ``sig``. A stage is pending
  exactly when its sig is not the file's current one, so a file replaced in
  place goes through every stage again without anything having to notice.
* Times are **naive UTC** for the instant (``taken_at``, what everything sorts
  by) and **naive wall clock** for where it was taken (``taken_local``, what the
  day headings group by). See core/timeutil.py.
* **Live Photos** are two files, a still and a two-second MOV, tied together by
  Apple's ``ContentIdentifier``. The still is the photo; the video is its
  companion, marked ``is_companion`` and left out of every listing, reachable
  only as the still's ``live_video_id``. Without that the grid shows every
  moment twice, once as a photo and once as a video of the same thing.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import embed
from .database import Base
from .timeutil import utcnow

# Where an undated photo sorts: after everything with a date, in a stable
# place, without a nullable sort key making every keyset comparison a
# three-valued one.
UNDATED = datetime(1900, 1, 1)  # noqa: DTZ001 - naive, like every stored time

# Where ``taken_at`` came from, best first. Kept per photo because "this date
# is a guess" is worth showing: a photo dated from its file time is dated by
# iCloud's upload, which is usually the day it was taken, and not always.
DATE_SOURCES = (
    "quicktime",    # Apple's com.apple.quicktime.creationdate, with its offset
    "exif",         # DateTimeOriginal (+ OffsetTimeOriginal when present)
    "xmp",          # XMP DateCreated
    "createdate",   # EXIF/QuickTime CreateDate: an encoder's, not a camera's
    "filename",     # IMG_20240101_123456, "Screenshot 2024-01-01 at ...", ...
    "mtime",        # the file's modification time, which rclone sets to iCloud's date
    "none",
)


class Photo(Base):
    """One file in the library."""

    __tablename__ = "photos"
    __table_args__ = (UniqueConstraint("root", "rel_path", name="uq_photo_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- the file ---
    root: Mapped[str] = mapped_column(String(64), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)          # photo | video
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # core.media.make_sig(root, rel_path, size, mtime_ns). The cache key.
    sig: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # --- when ---
    taken_at: Mapped[datetime | None] = mapped_column(DateTime)            # naive UTC
    taken_local: Mapped[datetime | None] = mapped_column(DateTime)         # naive wall clock
    tz_offset: Mapped[int | None] = mapped_column(Integer)                 # minutes east of UTC, when known
    date_source: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    # taken_at, or UNDATED. Never null; see ix_photos_timeline in database.py.
    sort_at: Mapped[datetime] = mapped_column(DateTime, default=UNDATED, nullable=False)

    # --- what ---
    # Pixel size as displayed, after the rotation the file asks for: a
    # portrait iPhone photo is stored 4032x3024 with an orientation tag, and is
    # 3024 wide as far as anything that lays out a grid is concerned.
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)                  # seconds, videos
    video_codec: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    make: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    lens: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    iso: Mapped[int | None] = mapped_column(Integer)
    f_number: Mapped[float | None] = mapped_column(Float)
    exposure_time: Mapped[float | None] = mapped_column(Float)             # seconds
    focal_length: Mapped[float | None] = mapped_column(Float)              # mm, as recorded
    is_screenshot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The rest of what exiftool said that is worth showing and not worth a
    # column: software, focal length in 35 mm, flash, and so on.
    exif: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # --- where ---
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    altitude: Mapped[float | None] = mapped_column(Float)
    # Reverse-geocoded offline from GeoNames' populated places (see
    # agent/geo.py): the nearest town, its region and its country. `place` is
    # those three joined for display and for `in:` searches.
    city: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    region: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    country: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    country_code: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    place: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # --- Live Photos ---
    content_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    # On the still: its motion. On the companion video: nothing.
    live_video_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL"), index=True
    )
    # On the companion video: True, and it is hidden from every listing.
    is_companion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- edits ---
    # iCloud hands over an edited photo as a second file, ``IMG_1925-edited.heic``
    # beside ``IMG_1925.HEIC``. Photos shows the edit and keeps the original
    # behind it; so does this. On the original: the edit that replaces it in
    # every listing. On the edit: nothing (``edited`` in the JSON is derived).
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL"), index=True
    )

    # --- what iCloud knows and the file does not ---
    # From ``rclone lsjson --metadata`` over the remote, refreshed after every
    # sync (see agent/albums.py). None of it is in the file: a heart set on
    # the phone, the Hidden album, Recently Deleted, the day a picture arrived.
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # In iCloud's Hidden album: hidden here too, unless asked for (is:hidden).
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # In iCloud's Recently Deleted. `rclone copy` never deletes, so the local
    # copy outlives the deletion; it is left out of every listing instead.
    icloud_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When it was added to the library (naive UTC): for a picture a messenger
    # saved, the moment it arrived, which its file cannot say.
    added_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Set when a delete was asked for here and the agent has not finished it
    # yet: gone from every listing at once, back if the delete fails. (Delete
    # is local: the photo stays in iCloud; see DeletedFile.)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # --- NSFW ---
    # Probability that the picture is sexually explicit, from a dedicated
    # classifier (agent/nsfw.py). For a video, the highest over its storyboard.
    nsfw: Mapped[float | None] = mapped_column(Float)
    # Marked safe by a person: never flagged, whatever ``nsfw`` says, and a
    # flagged photo that looks just like this one is not flagged either (see
    # app/query.py, ``safe_near``). The one column a person writes, not the
    # file or iCloud, so no reindex touches it.
    nsfw_safe: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- processing ---
    # Each holds the sig the stage last completed for; pending = differs.
    meta_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    thumb_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    preview_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # Videos only, companions excepted: a strip of frames for hover-scrubbing
    # (core.media.story_path), and how many frames it holds.
    story_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    story_frames: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nsfw_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # Stills only: the faces found in it are in ``faces`` (agent/faces.py).
    faces_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # The text read in it, if any, is in ``photo_texts`` (agent/ocr.py).
    ocr_sig: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # A stage that fails is retried, but not forever: three failures on one
    # version of a file park it until the file changes. The message is kept
    # because "why is this one grey" deserves an answer in the UI.
    error_stage: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class PhotoEmbedding(Base):
    """One photo's vector, for similarity and search.

    A table of its own rather than a column on ``photos``: the dimension is the
    model's, and changing model means dropping and recreating exactly this
    (see ``init_db``), not rewriting the table everything else lives in.
    """

    __tablename__ = "photo_embeddings"

    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    # Which model made it. A vector from another model with the same
    # dimension is not comparable, so a row whose model is not the configured
    # one counts as missing.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Which version of the file it was made from.
    sig: Mapped[str] = mapped_column(String(16), nullable=False)
    embedding = mapped_column(Vector(embed.dim()), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# The length of a face vector. ArcFace's, and every recognition model worth
# using since: unlike the search model's, this does not change with the model.
FACE_DIM = 512


class Face(Base):
    """One face in one photo, and its vector.

    Found and measured by the faces stage (agent/faces.py) on the version of
    the file ``sig`` names; the stage replaces a photo's faces wholesale
    whenever it runs on it again. ``pos`` numbers them largest first, and
    together with ``sig`` names the crop in the cache (core.media.face_path).
    There is no "person": two faces are the same person when their vectors
    are close enough, which is asked when somebody asks (``face:``), not
    decided ahead of time and stored.
    """

    __tablename__ = "faces"
    __table_args__ = (UniqueConstraint("photo_id", "pos", name="uq_face_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sig: Mapped[str] = mapped_column(String(16), nullable=False)
    pos: Mapped[int] = mapped_column(Integer, nullable=False)
    # Which model made the vector. Faces from another model are not
    # comparable with these, whatever their length.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # The face's box, as fractions of the photo as it is displayed (after
    # the rotation the file asks for): the same numbers at any size.
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    w: Mapped[float] = mapped_column(Float, nullable=False)
    h: Mapped[float] = mapped_column(Float, nullable=False)
    # The detector's confidence that this is a face.
    score: Mapped[float] = mapped_column(Float, nullable=False)
    embedding = mapped_column(Vector(FACE_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class PhotoText(Base):
    """The text in one photo or video, as the text stage read it.

    Only files with text have a row; ``photos.ocr_sig`` says a file was read
    at all. The stage replaces the row whenever it reads the file again.
    """

    __tablename__ = "photo_texts"

    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    # Which version of the file it was read from, and with what.
    sig: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # The lines joined by newlines, in reading order: what ``text:`` searches
    # (a trigram index, see database.py).
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # One object per line: ``t`` the text, ``s`` the recogniser's confidence,
    # and for a still ``b``, its box as [x, y, w, h] fractions of the photo as
    # displayed. A video's lines come from several frames and have no box.
    lines: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Album(Base):
    """An iCloud album, as rclone lists it beside "All Photos".

    ``kind`` is ``smart`` for the ones iCloud keeps by itself (Favorites,
    Screenshots, Recently Deleted, ...) and ``user`` for everything else,
    including the ones apps make (WhatsApp, Instagram). Membership is
    rebuilt wholesale on every refresh: iCloud is the truth.
    """

    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), default="user", nullable=False)
    # Members that matched a local file; the remote may hold more (not yet
    # synced, or a shared library this app does not copy).
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)


class PhotoAlbum(Base):
    __tablename__ = "photo_albums"

    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True, index=True
    )


# iCloud's own albums, by the names rclone gives them. Anything else is the
# user's (or an app's) and goes in the sidebar's Albums section.
SMART_ALBUMS = frozenset({
    "All Photos", "Animated", "Bursts", "Cinematic", "Favorites", "Hidden", "Live",
    "Long Exposure", "Panoramas", "Portrait", "RAW", "Recently Deleted", "Screenshots",
    "Selfies", "Slo-mo", "Spatial", "Time-lapse", "Videos",
})


class DeletedFile(Base):
    """A file Delete moved to the trash that iCloud still has.

    Delete only deletes here: rclone's iCloud Photos backend is read-only, so
    the photo stays in iCloud and on the phone, and without this row the next
    ``rclone copy`` would bring it straight back. Every sync excludes ``name``
    (agent/sync.py). The name is not the photo's for ever: while two assets
    share a name rclone suffixes both with their iCloud ids, so a new photo
    can rename this one, or, once this one is gone from iCloud too, take its
    plain name. So the content is kept as well: a file that arrives under
    another name with this ``sha256`` is this photo again, and the scan moves
    it to the trash too (agent/delete.py, ``returned``).
    """

    __tablename__ = "deleted_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root: Mapped[str] = mapped_column(String(64), nullable=False)
    # The name every sync excludes. None once a different photo holds it in
    # iCloud (agent/albums.py sees that in its listing): only the content is
    # recognised then.
    name: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # None when the file was already gone when Delete ran.
    sha256: Mapped[str | None] = mapped_column(String(64))
    # Where the file went. None once the trash was emptied; a file that is
    # no longer there although this is set was taken back out by hand, which
    # is how a delete is undone.
    trash: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

# Job kinds and states. Kept as strings, checked here, because the UI prints
# them and a Postgres enum is a migration every time one is added.
JOB_KINDS = ("sync", "scan", "preview", "reindex", "albums", "delete")
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")


class Job(Base):
    """Something the server has asked the agent to do.

    The web app holds no rclone credentials and runs no ffmpeg. The Sync button
    therefore leaves a row here, and the agent, which polls every couple of
    seconds, picks it up, runs it and writes its progress back into the same
    row, which is what the UI polls. The same shape as meercal's pending
    actions, and for the same reason.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    # kind-specific input: {"photo_id": 123} for a preview.
    params: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # kind-specific progress, replaced wholesale on every update. For a sync:
    # {"bytes", "total_bytes", "files", "total_files", "checks", "total_checks",
    #  "speed", "eta", "errors", "current": [names], "percent"}.
    progress: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # The last human-readable line, for the sidebar.
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # The tail of the job's output, capped (see agent.jobs.LOG_CAP).
    log: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)


class Setting(Base):
    """Small key/value state: UI preferences, the agent's heartbeat.

    Keys in use:
      ``prefs``         the UI's preferences, whatever the browser puts there
      ``agent.status``  {"version", "host", "seen_at", "phase", "workers",
                         "model", "pending": {...}} written by the agent
                         every loop; the UI's "is the agent alive" answer
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
