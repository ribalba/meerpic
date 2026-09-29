"""Where the photos were taken: the places list and the map's clusters.

The map never receives photos, only clusters: 16,000 located photos drawn as
16,000 markers is a browser that stops answering, and the one thing a map at
zoom 3 can say about them is how many are roughly where. So the database
counts, on a grid whose cells shrink as the map zooms in, and the browser
draws one marker per cell with the newest photo in it as its face.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import Photo

from ..query import (
    QuerySpec,
    bbox_clause,
    like_pattern,
    listed,
    matches,
    normalize_bbox,
    parse_query,
    resolve_scoring,
)
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["places"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]

PLACES_DEFAULT = 20
PLACES_MAX = 200
MAX_ZOOM = 24


@router.get("/places")
def places(db: DB, q: str = "", limit: int = PLACES_DEFAULT) -> dict:
    """Places with photos, most photographed first; ``q`` narrows by name."""
    limit = max(1, min(limit, PLACES_MAX))
    n = func.count().label("n")
    stmt = (
        select(
            Photo.place, Photo.city, Photo.region, Photo.country, n,
            func.avg(Photo.lat).label("lat"), func.avg(Photo.lon).label("lon"),
        )
        .where(*listed(QuerySpec()), Photo.place != "")
        .group_by(Photo.place, Photo.city, Photo.region, Photo.country)
        .order_by(n.desc(), Photo.place)
        .limit(limit)
    )
    if q.strip():
        stmt = stmt.where(Photo.place.ilike(like_pattern(q.strip()), escape="\\"))
    return {
        "places": [
            {
                "place": r.place,
                "city": r.city,
                "region": r.region,
                "country": r.country,
                "count": int(r.n),
                "lat": round(r.lat, 6) if r.lat is not None else None,
                "lon": round(r.lon, 6) if r.lon is not None else None,
            }
            for r in db.execute(stmt).all()
        ]
    }


def parse_bbox(text: str | None) -> tuple[float, float, float, float]:
    if not text:
        return -180.0, -90.0, 180.0, 90.0
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise HTTPException(400, "bbox= takes west,south,east,north") from exc
    box = normalize_bbox(w, s, e, n)
    if box is None:
        raise HTTPException(400, "bbox= takes west,south,east,north")
    return box


def cell_degrees(zoom: float) -> float:
    """The grid's cell size: a quarter of a map tile's width at this zoom, so
    a cluster marker (about 50 px) never has a neighbour closer than about
    one marker's width."""
    z = max(0, min(int(zoom), MAX_ZOOM))
    return 360.0 / (2 ** z) / 4


@router.get("/map/clusters")
def clusters(db: DB, bbox: str | None = None, zoom: float = 3, q: str = "") -> dict:
    w, s, e, n = parse_bbox(bbox)
    cell = cell_degrees(zoom)
    spec = parse_query(q)
    clauses = [*listed(spec), Photo.lat.is_not(None), Photo.lon.is_not(None), bbox_clause(w, s, e, n)]
    if spec.scored:
        # The map of a search shows where the matches are: above the floor,
        # within the cap, and the cap counted inside this box. That is the
        # set "Show in grid" lists, which puts this same box into the query.
        scoring, _ = resolve_scoring(db, spec)
        if scoring is None:
            return {"clusters": [], "total": 0}
        ranked = matches(scoring, list(clauses))
        clauses.append(Photo.id.in_(select(ranked.c.id)))

    has_thumb = (Photo.thumb_sig == Photo.sig) & (Photo.thumb_sig != "")
    # Cell numbers computed once in a subquery and grouped on as plain
    # columns: grouping on an expression with a bound parameter in it makes
    # Postgres compare two parameters it cannot prove equal.
    members = select(
        Photo.id, Photo.lat, Photo.lon, Photo.sort_at, has_thumb.label("has_thumb"),
        func.floor(Photo.lat / cell).label("gy"),
        func.floor(Photo.lon / cell).label("gx"),
    ).where(*clauses).subquery()
    m = members.c
    # The face of a cluster: its newest photo that has a thumbnail, or its
    # newest photo when none has one yet. One ordered array per cell, first
    # element, rather than a second query per cell.
    face = func.array_agg(aggregate_order_by(m.id, m.has_thumb.desc(), m.sort_at.desc(), m.id.desc()))[1]
    count = func.count().label("count")
    rows = db.execute(
        select(
            count,
            func.avg(m.lat).label("lat"), func.avg(m.lon).label("lon"),
            func.min(m.lat).label("s"), func.max(m.lat).label("n"),
            func.min(m.lon).label("w"), func.max(m.lon).label("e"),
            face.label("face"),
        )
        .group_by(m.gy, m.gx)
        .order_by(count.desc())
    ).all()

    faces = {r.face for r in rows}
    sigs = {
        pid: sig
        for pid, sig in db.execute(
            select(Photo.id, Photo.sig).where(Photo.id.in_(faces), Photo.thumb_sig == Photo.sig, Photo.thumb_sig != "")
        ).all()
    } if faces else {}
    out = [
        {
            "lat": round(r.lat, 6),
            "lon": round(r.lon, 6),
            "count": int(r.count),
            "id": r.face,
            "thumb": f"/media/thumb/{r.face}?v={sigs[r.face]}" if r.face in sigs else None,
            "bbox": [round(r.w, 6), round(r.s, 6), round(r.e, 6), round(r.n, 6)],
        }
        for r in rows
    ]
    return {"clusters": out, "total": sum(c["count"] for c in out)}
