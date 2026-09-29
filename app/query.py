"""The filter language, and the queries every view is built from.

The bar at the top of the window is the whole state of the grid: its text goes
into the URL, and every list, the timeline and the map read the same string.
That is meercal's idiom (and meerail's before it), with the nouns of a photo
library instead of a calendar's:

    cows in:Potsdam year:2024 is:video

* **Words** are not matched against anything: they are what the photo shows,
  turned into a vector by the search model (see core/embed.py) and compared
  with every photo's. A photo "matches" when it is close enough, which is why
  a search is a ranking with a floor, never a yes or no.
* **Filters** are ``key:value`` and narrow whatever the words found, or the
  whole library when there are no words. Different filters are ANDed, the same
  one given twice is ANDed too: ``in:Berlin in:Potsdam`` is nothing, which is
  what it says.
* ``"double quotes"`` keep a phrase together, and ``in:"New York"`` works the
  same way. A token that is quoted as a whole is always words, so
  ``"in:the woods"`` can still be searched for.
* A key this parser does not know is reported and ignored, never guessed at:
  a typo must not silently turn into a search for the typo.
* Some photos are left out of every listing: iCloud's Hidden album and
  Recently Deleted unless asked for by name (``is:hidden``, ``is:deleted``),
  and always the original behind an edit and whatever a Delete here is still
  busy with. :func:`listed` is the one place that says so, and everything
  that lists or counts goes through it.

Two halves, kept apart on purpose. :func:`parse_query` is pure and turns text
into a :class:`QuerySpec`. :func:`filter_clauses` and :func:`matches` turn a
spec into SQLAlchemy expressions, with every value from the user a bound
parameter. Only :func:`resolve_scoring` touches a database, to fetch the
vector a ``similar:`` or ``face:`` search starts from.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from core import embed
from core.config import get_settings
from core.database import SessionLocal
from core.models import Album, Face, Photo, PhotoAlbum, PhotoEmbedding, PhotoText

# `is:` values. Kept as a tuple so the help text and the tests can list them.
IS_FLAGS = (
    "photo", "video", "live", "screenshot", "located", "unlocated", "dated", "undated",
    "favorite", "hidden", "deleted", "whatsapp", "saved", "nsfw", "safe", "edited",
)
# Other spellings of a flag, written back in the canonical one. The heart is
# a "favourite" to half the people who will type it.
IS_ALIASES = {"favourite": "favorite"}
SORTS = ("date", "score")
KEYS = (
    "in", "near", "bbox", "is", "year", "month", "on", "after", "before",
    "camera", "file", "album", "similar", "face", "sort", "text",
)
# The album a messenger saves into. WhatsApp strips every tag from what it
# saves, so membership of its album is the only thing that says where a
# picture came from.
WHATSAPP_ALBUM = "WhatsApp"

# A search is a ranking, and the tail of a ranking is noise long before it is
# empty: past a couple of thousand, "photos like this one" is most of the
# library in a slightly different order. The cap also bounds the one query
# that has to look at every vector.
SCORE_CAP = 2000

# Mean Earth radius (IUGG), for great-circle distance.
EARTH_KM = 6371.0088
NEAR_DEFAULT_KM = 1.0
NEAR_MAX_KM = 20038.0          # half the circumference: everywhere
KM_PER_DEGREE_LAT = 111.19

# Quote characters. The typographic pair is there because a Mac left to its
# own devices turns a typed " into one of them.
_QUOTES = {'"', "“", "”", "„"}
_KEY = re.compile(r"^([A-Za-z]+):(.*)$", re.DOTALL)
_PERIOD = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")
_YEAR = re.compile(r"^\d{4}$")
MIN_YEAR, MAX_YEAR = 1900, 2100


@dataclass(frozen=True)
class Token:
    text: str
    # True when the token opened with a quote: then it is words, whatever it
    # looks like.
    quoted: bool = False


@dataclass(frozen=True)
class Filter:
    """One recognised filter.

    ``key`` is the kind of condition (``in``, ``near``, ``bbox``, ``is``,
    ``taken``, ``camera``, ``file``), ``value`` is already parsed and
    checked, and ``label`` is the canonical way to write it, which is what the
    UI is shown back (``2024`` comes back as ``year:2024``).
    """

    key: str
    value: Any
    label: str


@dataclass
class QuerySpec:
    words: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    similar: int | None = None
    # A face's id: the photos with the same person in them (agent/faces.py).
    face: int | None = None
    sort: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The words, as the search model is given them."""
        return " ".join(self.words)

    @property
    def flags(self) -> set[str]:
        """The ``is:`` values asked for."""
        return {f.value for f in self.filters if f.key == "is"}

    @property
    def scored(self) -> bool:
        """Whether this is a ranking by vector (words, similar: or face:)
        rather than a timeline."""
        return self.similar is not None or self.face is not None or bool(self.words)

    @property
    def nsfw_ranked(self) -> bool:
        """``is:nsfw`` on its own is a ranking too, by the classifier's
        certainty: the few it is sure about first, not whichever came last."""
        return not self.scored and "nsfw" in self.flags

    @property
    def mode(self) -> str:
        return "score" if (self.scored or self.nsfw_ranked) else "date"

    @property
    def by_date(self) -> bool:
        """A ranking the user asked to see in date order (``sort:date``)."""
        return self.mode == "score" and self.sort == "date"

    def describe(self) -> dict:
        """The ``query`` object of an API answer: what was understood."""
        return {
            "text": self.text,
            "similar": self.similar,
            "face": self.face,
            "filters": [f.label for f in self.filters],
            "errors": list(self.errors),
        }


# --- tokens -------------------------------------------------------------------


def tokenize(raw: str) -> list[Token]:
    """Split on whitespace, keeping double-quoted runs together.

    Written by hand rather than with shlex: shlex also gives meaning to single
    quotes and backslashes, and "Mc'Donald's" or a Windows path in the search
    bar is text, not a syntax error. An unclosed quote runs to the end, which
    is what the bar holds while someone is still typing it.
    """
    tokens: list[Token] = []
    buf: list[str] = []
    started = quoted = in_quote = False
    for ch in raw or "":
        if in_quote:
            if ch in _QUOTES:
                in_quote = False
            else:
                buf.append(ch)
        elif ch in _QUOTES:
            if not started:
                quoted = True
            started = in_quote = True
        elif ch.isspace():
            if started:
                tokens.append(Token("".join(buf), quoted))
            buf, started, quoted = [], False, False
        else:
            buf.append(ch)
            started = True
    if started:
        tokens.append(Token("".join(buf), quoted))
    return [t for t in tokens if t.text.strip()]


def _quote(value: str) -> str:
    return f'"{value}"' if (not value or any(c.isspace() for c in value)) else value


def _num(value: float) -> str:
    """A coordinate as the shortest text that round-trips to 6 decimals."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


# --- values -------------------------------------------------------------------


def midnight(day: date) -> datetime:
    """The start of a day as a naive wall clock, which is what ``taken_local``
    is: a date filter compares with the clock where the photo was taken, not
    with an instant in any zone."""
    return datetime.combine(day, time())


def parse_period(text: str) -> tuple[datetime, datetime] | None:
    """``2024``, ``2024-07`` or ``2024-07-22`` as the half-open range it names."""
    m = _PERIOD.match(text.strip())
    if not m:
        return None
    year = int(m[1])
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    if m[2] is None:
        return midnight(date(year, 1, 1)), midnight(date(year + 1, 1, 1))
    month = int(m[2])
    if not 1 <= month <= 12:
        return None
    if m[3] is None:
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return midnight(start), midnight(end)
    try:
        day = date(year, month, int(m[3]))
    except ValueError:
        return None
    return midnight(day), midnight(day + timedelta(days=1))


def _floats(text: str, count: tuple[int, ...]) -> list[float] | None:
    parts = [p.strip() for p in text.replace(" ", "").split(",")]
    if len(parts) not in count:
        return None
    out = []
    for i, part in enumerate(parts):
        # `near:52.4,13.1,5km` reads naturally; the unit is the only one.
        if i == 2 and part.lower().endswith("km"):
            part = part[:-2]
        try:
            value = float(part)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        out.append(value)
    return out


def wrap_lon(lon: float) -> float:
    """A longitude as -180..180. A map panned once round the world reports 190."""
    if -180.0 <= lon <= 180.0:
        return lon
    return ((lon + 180.0) % 360.0) - 180.0


def normalize_bbox(w: float, s: float, e: float, n: float) -> tuple[float, float, float, float] | None:
    """A box as (w, s, e, n) with longitudes in -180..180, or None if it is not one.

    ``w > e`` after this means the box crosses the antimeridian (Fiji, the
    Bering Strait, a map scrolled sideways), which the SQL below reads as two
    ranges. A box 360 degrees wide or more is the whole world.
    """
    if s > n:
        return None
    s, n = max(-90.0, s), min(90.0, n)
    if s > n:
        return None
    if e - w >= 360.0:
        return -180.0, s, 180.0, n
    if w > e and -180.0 <= w <= 180.0 and -180.0 <= e <= 180.0:
        return w, s, e, n            # already crossing, as given
    if w > e:
        return None
    return wrap_lon(w), s, wrap_lon(e), n


# --- the parser ---------------------------------------------------------------


def parse_query(raw: str) -> QuerySpec:
    """``cows in:Potsdam 2024``, in that or any other order. Never raises."""
    spec = QuerySpec()
    for token in tokenize(raw):
        m = None if token.quoted else _KEY.match(token.text)
        if m is None:
            text = token.text.strip()
            period = parse_period(text) if (not token.quoted and _YEAR.match(text)) else None
            if period:
                spec.filters.append(Filter("taken", period, f"year:{text}"))
            else:
                spec.words.append(text)
            continue
        key, value = m[1].lower(), m[2].strip()
        if key not in KEYS:
            spec.errors.append(f'unknown filter "{token.text}"')
            continue
        if not value:
            # `in:` with nothing after it is somebody half way through
            # typing, not a mistake worth a message.
            continue
        _parse_filter(spec, key, value)

    if spec.similar is not None and spec.face is not None:
        spec.errors.append(f"face:{spec.face} ignored with similar:")
        spec.face = None
    if spec.similar is not None and spec.words:
        spec.errors.append(f"ignored with similar: {spec.text}")
        spec.words = []
    if spec.face is not None and spec.words:
        spec.errors.append(f"ignored with face: {spec.text}")
        spec.words = []
    return spec


def _parse_filter(spec: QuerySpec, key: str, value: str) -> None:
    raw = f"{key}:{value}"
    if key in ("in", "camera", "file", "album", "text"):
        spec.filters.append(Filter(key, value, f"{key}:{_quote(value)}"))
    elif key == "is":
        flag = value.lower()
        flag = IS_ALIASES.get(flag, flag)
        if flag in IS_FLAGS:
            spec.filters.append(Filter("is", flag, f"is:{flag}"))
        else:
            spec.errors.append(f'"{raw}": is: takes {", ".join(IS_FLAGS)}')
    elif key in ("year", "month", "on", "after", "before"):
        shape = {"year": 1, "month": 2, "on": 3}.get(key)
        period = parse_period(value)
        if period is None or (shape and value.count("-") + 1 != shape):
            example = {"year": "2024", "month": "2024-07", "on": "2024-07-22"}.get(key, "2024-07-22")
            spec.errors.append(f'"{raw}" is not a date like {key}:{example}')
            return
        start, end = period
        rng = (start, None) if key == "after" else (None, end) if key == "before" else (start, end)
        # Written back the way it is meant: month:2024-7 is month:2024-07.
        canonical = start.strftime(("%Y", "%Y-%m", "%Y-%m-%d")[value.count("-")])
        spec.filters.append(Filter("taken", rng, f"{key}:{canonical}"))
    elif key == "near":
        nums = _floats(value, (2, 3))
        if nums is None:
            spec.errors.append(f'"{raw}": near: takes lat,lon or lat,lon,km')
            return
        lat, lon = nums[0], nums[1]
        km = nums[2] if len(nums) == 3 else NEAR_DEFAULT_KM
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < km <= NEAR_MAX_KM):
            spec.errors.append(f'"{raw}": out of range')
            return
        label = f"near:{_num(lat)},{_num(lon)}" + (f",{_num(km)}" if len(nums) == 3 else "")
        spec.filters.append(Filter("near", (lat, lon, km), label))
    elif key == "bbox":
        nums = _floats(value, (4,))
        box = normalize_bbox(*nums) if nums else None
        if box is None:
            spec.errors.append(f'"{raw}": bbox: takes west,south,east,north')
            return
        spec.filters.append(Filter("bbox", box, "bbox:" + ",".join(_num(v) for v in box)))
    elif key == "similar":
        try:
            photo_id = int(value)
        except ValueError:
            photo_id = 0
        if photo_id <= 0:
            spec.errors.append(f'"{raw}": similar: takes a photo id')
        elif spec.similar is not None and spec.similar != photo_id:
            spec.errors.append(f'"{raw}": only one similar: at a time')
        else:
            spec.similar = photo_id
    elif key == "face":
        try:
            face_id = int(value)
        except ValueError:
            face_id = 0
        if face_id <= 0:
            spec.errors.append(f'"{raw}": face: takes a face id')
        elif spec.face is not None and spec.face != face_id:
            spec.errors.append(f'"{raw}": only one face: at a time')
        else:
            spec.face = face_id
    elif key == "sort":
        order = value.lower()
        if order in SORTS:
            spec.sort = order
        else:
            spec.errors.append(f'"{raw}": sort: takes date or score')


# --- SQL ----------------------------------------------------------------------


def like_pattern(text: str) -> str:
    """``%text%`` with the user's own ``%`` and ``_`` taken literally.

    A file name like IMG_1234 has an underscore in it, and ILIKE reads that as
    "any character". Escaped here rather than with ``icontains``, which wraps
    the column in lower() and so cannot use the trigram index.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ilike(column, text: str):
    return column.ilike(like_pattern(text), escape="\\")


def album_clause(name: str):
    """Members of the album called ``name``, whatever its case.

    ``IN (subquery)`` rather than a join: a join would repeat a photo that is
    in two albums of the same name folded to lower case, and Postgres turns
    the membership list into a hash it probes once per row, which is what
    keeps the grid's walk down the timeline index cheap.
    """
    return Photo.id.in_(
        select(PhotoAlbum.photo_id)
        .join(Album, Album.id == PhotoAlbum.album_id)
        .where(func.lower(Album.name) == func.lower(name))
    )


def text_clause(value: str):
    """Photos whose text (agent/ocr.py) holds every word of ``value``, each
    anywhere in it and in any case: ``text:"opening hours"`` also finds the
    sign where the two words are on lines of their own, which is how a sign
    is usually read. A part of a word is enough (``text:rechn``), and a
    reading slip in the rest of the word does not get in the way.
    """
    words = value.split()
    return Photo.id.in_(
        select(PhotoText.photo_id).where(*(_ilike(PhotoText.text, w) for w in words))
    )


def edited_clause():
    """Photos that some other photo is the original of: iCloud's edits."""
    original = aliased(Photo)
    return Photo.id.in_(select(original.superseded_by).where(original.superseded_by.is_not(None)))


def saved_clause():
    """Pictures an app or a browser saved: a still with no camera maker, and
    not a screenshot, which has none either but is a thing of its own."""
    return and_(Photo.kind == "photo", Photo.make == "", ~Photo.is_screenshot)


def safe_ids(db: Session | None = None) -> list[int]:
    """The photos marked safe, a handful (ix_photos_nsfw_safe). Looked up in
    ``db`` when given, which sees that transaction's own marks, else in a
    short session of its own."""
    stmt = select(Photo.id).where(Photo.nsfw_safe.is_(True))
    if db is not None:
        return list(db.scalars(stmt))
    with SessionLocal() as own:
        return list(own.scalars(stmt))


def safe_near(similarity: float, ids: list[int]):
    """Whether this photo (correlated on ``photos.id``) looks like one of the
    marked ``ids``: its search vector at least ``similarity`` close (cosine).

    The ids are looked up first (safe_ids) and callers skip this when there
    are none. A subquery that found the marked photos itself was planned the
    wrong way round: every flagged photo against every vector in the library
    before asking which were marked, 46 s for /api/state on 19,000 photos.
    This way both vectors are primary-key lookups."""
    me, them = aliased(PhotoEmbedding), aliased(PhotoEmbedding)
    return (
        select(them.photo_id)
        .join(me, and_(me.photo_id == Photo.id, me.model == them.model))
        .where(
            them.photo_id.in_(ids), them.photo_id != Photo.id, them.model == embed.model_name(),
            me.embedding.cosine_distance(them.embedding) <= 1 - similarity,
        )
        .exists()
    )


def nsfw_clause(ids: list[int] | None = None):
    """Flagged: above the threshold, and neither marked safe nor looking just
    like a photo that was (``ids``, the marked ones; looked up when None).
    Read at query time, not import time, so changed settings (and a test that
    changes them) take effect without a restart."""
    s = get_settings()
    clause = and_(Photo.nsfw >= s.nsfw_threshold, Photo.nsfw_safe.is_(False))
    if s.nsfw_clear_above > 0:
        ids = safe_ids() if ids is None else ids
        if ids:
            clause = and_(clause, ~safe_near(s.nsfw_clear_above, ids))
    return clause


def safe_cleared(db: Session, photos: list) -> set[int]:
    """Which of these photos the classifier flags, and look just like a photo
    marked safe, so are not flagged after all. One query per page, none when
    nothing on it is flagged or nothing is marked."""
    s = get_settings()
    flagged = [p.id for p in photos
               if p.nsfw is not None and p.nsfw >= s.nsfw_threshold and not p.nsfw_safe]
    if not flagged or s.nsfw_clear_above <= 0:
        return set()
    marked = safe_ids(db)
    if not marked:
        return set()
    return set(db.scalars(
        select(Photo.id).where(Photo.id.in_(flagged), safe_near(s.nsfw_clear_above, marked))
    ))


def safe_lookalike(db: Session, photo: Photo) -> tuple[Photo, float] | None:
    """The photo marked safe that this one looks most like, and how much, for
    the info panel; None when none is similar enough to matter."""
    s = get_settings()
    floor = min((x for x in (s.nsfw_clear_above, s.nsfw_demote_above) if x > 0), default=None)
    if floor is None or photo.nsfw is None or photo.nsfw < s.nsfw_threshold or photo.nsfw_safe:
        return None
    marked = [i for i in safe_ids(db) if i != photo.id]
    if not marked:
        return None
    me, them = aliased(PhotoEmbedding), aliased(PhotoEmbedding)
    model = embed.model_name()
    distance = me.embedding.cosine_distance(them.embedding)
    row = db.execute(
        select(Photo, distance)
        .join(them, them.photo_id == Photo.id)
        .join(me, and_(me.photo_id == photo.id, me.model == them.model))
        .where(them.photo_id.in_(marked), me.model == model, distance <= 1 - floor)
        .order_by(distance)
        .limit(1)
    ).first()
    return (row[0], 1 - float(row[1])) if row else None


# Each is built when asked for: two of them are subqueries, and one reads the
# settings.
_FLAG_CLAUSES = {
    "photo": lambda: Photo.kind == "photo",
    "video": lambda: Photo.kind == "video",
    "live": lambda: Photo.live_video_id.is_not(None),
    "screenshot": lambda: Photo.is_screenshot.is_(True),
    "located": lambda: Photo.lat.is_not(None),
    "unlocated": lambda: Photo.lat.is_(None),
    # On the wall clock, because that is what the timeline groups on and
    # what a date filter compares: "undated" is the timeline's own bucket.
    "dated": lambda: Photo.taken_local.is_not(None),
    "undated": lambda: Photo.taken_local.is_(None),
    "favorite": lambda: Photo.favorite.is_(True),
    # These two only select; what lifts their exclusion is listed().
    "hidden": lambda: Photo.hidden.is_(True),
    "deleted": lambda: Photo.icloud_deleted.is_(True),
    "whatsapp": lambda: album_clause(WHATSAPP_ALBUM),
    "saved": saved_clause,
    "nsfw": nsfw_clause,
    # Marked safe by hand (the marks, not the lookalikes they clear).
    "safe": lambda: Photo.nsfw_safe.is_(True),
    "edited": edited_clause,
}


def flag_clause(flag: str):
    return _FLAG_CLAUSES[flag]()


def near_clause(lat: float, lon: float, km: float):
    """Great-circle distance (haversine) within ``km``.

    The latitude band in front is only there so the (lat, lon) index can throw
    most of the library away before the trigonometry runs.
    """
    band = km / KM_PER_DEGREE_LAT
    dlat = func.radians(Photo.lat - lat)
    dlon = func.radians(Photo.lon - lon)
    a = (
        func.power(func.sin(dlat / 2), 2)
        + math.cos(math.radians(lat)) * func.cos(func.radians(Photo.lat)) * func.power(func.sin(dlon / 2), 2)
    )
    distance = 2 * EARTH_KM * func.asin(func.least(1.0, func.sqrt(a)))
    return and_(
        Photo.lat.between(lat - band, lat + band),
        Photo.lon.is_not(None),
        distance <= km,
    )


def bbox_clause(w: float, s: float, e: float, n: float):
    lat_ok = Photo.lat.between(s, n)
    if w <= e:
        return and_(lat_ok, Photo.lon.between(w, e))
    # Across the antimeridian: two ranges, one each side of it.
    return and_(lat_ok, or_(Photo.lon >= w, Photo.lon <= e))


def period_clause(start: datetime | None, end: datetime | None):
    parts = []
    if start is not None:
        parts.append(Photo.taken_local >= start)
    if end is not None:
        parts.append(Photo.taken_local < end)
    return and_(*parts)


def filter_clause(f: Filter):
    if f.key == "in":
        return _ilike(Photo.place, f.value)
    if f.key == "camera":
        return or_(_ilike(Photo.make, f.value), _ilike(Photo.model, f.value))
    if f.key == "file":
        return _ilike(Photo.name, f.value)
    if f.key == "album":
        return album_clause(f.value)
    if f.key == "text":
        return text_clause(f.value)
    if f.key == "is":
        return flag_clause(f.value)
    if f.key == "taken":
        return period_clause(*f.value)
    if f.key == "near":
        return near_clause(*f.value)
    if f.key == "bbox":
        return bbox_clause(*f.value)
    raise ValueError(f"no clause for filter {f.key!r}")


def filter_clauses(spec: QuerySpec) -> list:
    """Everything the filters ask for. Words and similar: are not filters."""
    return [filter_clause(f) for f in spec.filters]


def never_listed() -> list:
    """What no listing shows, whatever the query says.

    A Live Photo's motion half, reached through its still and nowhere else;
    the original behind an edit, reached through the edit ("Show original");
    and a photo a Delete here has taken off the screen and the agent has not
    finished deleting yet.
    """
    return [~Photo.is_companion, Photo.superseded_by.is_(None), Photo.trashed_at.is_(None)]


def listed(spec: QuerySpec) -> list:
    """What the grid may show at all: the filters, minus :func:`never_listed`,
    and minus iCloud's Hidden album and Recently Deleted unless the query
    asks for exactly those (``is:hidden``, ``is:deleted``), as Photos does.

    Spelled ``NOT is_companion`` and not ``is_companion IS false``, although
    the column cannot be null: the timeline index is partial on exactly
    ``WHERE NOT is_companion``, and Postgres only uses a partial index when it
    can prove the query implies the predicate. With the other spelling it
    cannot, and every page of the grid became a sequential scan. The rest are
    plain filters on that walk: each leaves out a few rows in a thousand.
    """
    flags = spec.flags
    clauses = never_listed()
    if "hidden" not in flags:
        clauses.append(~Photo.hidden)
    if "deleted" not in flags:
        clauses.append(~Photo.icloud_deleted)
    return [*clauses, *filter_clauses(spec)]


# --- scoring ------------------------------------------------------------------


@dataclass
class Scoring:
    """What a ranking needs: the vector to compare with and where noise starts."""

    vector: Any
    threshold: float
    exclude: int | None = None
    # A face: search compares with faces, not with whole pictures.
    face: Face | None = None


@lru_cache(maxsize=64)
def _text_vector(model: str, text: str):
    # Paging through a search asks for the same words every page, and a SigLIP
    # text pass is ~60 ms. `model` is in the key so a changed configuration
    # cannot answer with a vector from the other model.
    return embed.embed_text(text)


def resolve_scoring(db: Session, spec: QuerySpec) -> tuple[Scoring | None, Photo | None]:
    """The vector for this query, or None with the reason added to ``spec.errors``.

    Returns the source photo of a ``similar:`` or ``face:`` too, for the
    header above the ranking, even when it has no vector yet.
    """
    if spec.face is not None:
        face = db.get(Face, spec.face)
        source = db.get(Photo, face.photo_id) if face is not None else None
        if face is None or source is None or face.sig != source.sig:
            # Its photo changed and was looked at again, which numbers the
            # faces anew.
            spec.errors.append(f"face:{spec.face}: there is no such face (any more)")
            return None, source
        # The photo the face is in stays in, first: "pictures with this face"
        # that leaves out the one it was clicked in says 0 for somebody who
        # is in one picture only.
        return Scoring(face.embedding, get_settings().faces_min_score, face=face), source
    if spec.similar is not None:
        source = db.get(Photo, spec.similar)
        if source is None:
            spec.errors.append(f"similar:{spec.similar}: there is no such photo")
            return None, None
        vector = db.execute(
            select(PhotoEmbedding.embedding).where(
                PhotoEmbedding.photo_id == source.id,
                PhotoEmbedding.model == embed.model_name(),
            )
        ).scalar()
        if vector is None:
            spec.errors.append(f"similar:{spec.similar}: this photo is not indexed for search yet")
            return None, source
        return Scoring(vector, embed.similar_min_score(), exclude=source.id), source
    if spec.words:
        try:
            vector = _text_vector(embed.model_name(), spec.text)
        # Anything at all: a download that failed, a model that will not
        # load. The grid answers with the reason rather than a 500.
        except Exception as exc:  # noqa: BLE001
            spec.errors.append(f"search is unavailable: {type(exc).__name__}: {exc}"[:300])
            return None, None
        return Scoring(vector, embed.min_score()), None
    return None, None


def matches(scoring: Scoring, clauses: list):
    """The ranking as a subquery ``(id, score)``: above the floor, best first,
    at most ``SCORE_CAP`` rows.

    An exact scan over every vector, deliberately. The HNSW index answers
    "the nearest k" and nothing else: it cannot count what is above a
    threshold, and a filtered index scan returns only what its candidate list
    happens to hold (40 by default) with no sign that anything is missing. At
    25,000 photos the exact scan is well inside the time a keystroke allows.
    Ordering by the *score* rather than by ``embedding <=> v`` is also what
    keeps the planner from picking the index for the ORDER BY.
    """
    if scoring.face is not None:
        return face_matches(scoring, clauses)
    distance = PhotoEmbedding.embedding.cosine_distance(scoring.vector)
    score = (1.0 - distance).label("score")
    stmt = (
        select(PhotoEmbedding.photo_id.label("id"), score)
        .join(Photo, Photo.id == PhotoEmbedding.photo_id)
        .where(
            PhotoEmbedding.model == embed.model_name(),
            distance <= 1.0 - scoring.threshold,
            *clauses,
        )
        .order_by(score.desc(), PhotoEmbedding.photo_id)
        .limit(SCORE_CAP)
    )
    if scoring.exclude is not None:
        stmt = stmt.where(Photo.id != scoring.exclude)
    return stmt.subquery("matches")


def face_matches(scoring: Scoring, clauses: list):
    """``face:`` as a ranking subquery ``(id, score)``: the photos with a face
    close enough to this one, each scored by its closest face, best first.

    An exact scan over every face, for the reasons :func:`matches` gives, and
    there is no vector index on faces to tempt the planner. A face counts
    only if it was found in the photo's current version, by the model that
    found this one: another model's vectors live in another space.
    """
    distance = Face.embedding.cosine_distance(scoring.vector)
    score = func.max(1.0 - distance).label("score")
    stmt = (
        select(Face.photo_id.label("id"), score)
        .join(Photo, Photo.id == Face.photo_id)
        .where(
            Face.model == scoring.face.model,
            Face.sig == Photo.sig,
            distance <= 1.0 - scoring.threshold,
            *clauses,
        )
        .group_by(Face.photo_id)
        .order_by(score.desc(), Face.photo_id)
        .limit(SCORE_CAP)
    )
    if scoring.exclude is not None:
        stmt = stmt.where(Face.photo_id != scoring.exclude)
    return stmt.subquery("matches")


def nsfw_matches(clauses: list):
    """``is:nsfw`` as a ranking subquery ``(id, score)``, the score being the
    classifier's probability, shaped like :func:`matches` so the listing
    treats the two alike.

    Not capped: this set is a few dozen photos in a real library (the
    threshold is the floor, and the filter in ``clauses`` applies it), and
    every one of them should be reachable, which is the point of the view.

    A photo that looks somewhat like one marked safe (``nsfw.demote_above``)
    scores its probability minus one, which lists it after every other.
    """
    score = Photo.nsfw
    demote = get_settings().nsfw_demote_above
    marked = safe_ids() if demote > 0 else []
    if marked:
        score = case((safe_near(demote, marked), Photo.nsfw - 1), else_=Photo.nsfw)
    return select(Photo.id.label("id"), score.label("score")).where(*clauses).subquery("matches")
