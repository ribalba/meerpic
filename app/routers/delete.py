"""Delete: into the trash here; the photo stays in iCloud.

rclone cannot delete in iCloud Photos (its backend there is read-only), so a
Delete moves the files to ``.meerpic-trash`` in the library folder and keeps
every later sync from downloading them again (agent/delete.py). The server
cannot do even that: the library is mounted read-only into it (see
app/main.py). What it does is the part a person has to see and agree to, and
the part that must happen at once.

* ``POST /api/photos/delete/plan`` says exactly what a Delete would do: every
  file of every chosen photo (a Live Photo's motion, an edit's original),
  where each one moves, and which names later syncs leave in iCloud. It
  reads and changes nothing. The confirmation dialog shows it.
* ``POST /api/photos/delete`` carries the moves the dialog showed. The plan
  is made again, and if it is not the one that was shown (a sync or an
  albums refresh changed something in between), nothing happens and the new
  plan comes back to be shown instead. Otherwise every file of the plan
  leaves every listing at once (``trashed_at``, see query.never_listed), and
  one ``delete`` job hands the agent the plan itself, which is all it runs.
  A delete that fails clears ``trashed_at`` again, and the photos come back.

The grouping is core.deletion's, shared with the agent, so what was shown and
what runs cannot drift apart.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.deletion import MAX_PHOTOS, Plan, plan
from core.models import Job, Photo
from core.timeutil import utcnow

from ..security import require_auth
from ..serialize import deletable, job_json

router = APIRouter(prefix="/api", tags=["delete"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]
settings = get_settings()

# See photos.LOCK_PREVIEW and jobs.LOCK_SYNC: two Deletes of the same photos
# (a double click, two windows) are made one after the other, so the second
# sees the first's trashed_at and its plan no longer matches.
LOCK_DELETE = 0x6D72_6430     # "mrd0"

DELETE_OFF = "Delete is turned off ([library] delete = false in meerpic.toml)"
CHANGED = "The photos changed since this was shown: here is what Delete would do now"


class PlanBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_PHOTOS)


class DeleteBody(PlanBody):
    # What the dialog showed. Required: nothing is moved that nobody saw.
    moves: list[str]
    # The names it said later syncs will leave in iCloud, when the client
    # sends them too.
    excluded: list[str] | None = None


def _require_delete() -> None:
    if not deletable():
        raise HTTPException(400, DELETE_OFF)


def plan_json(p: Plan) -> dict:
    """The plan as the dialog shows it, with how many files it touches."""
    return {**p.to_json(), "count": len(p.photo_ids)}


@router.post("/photos/delete/plan")
def delete_plan(body: PlanBody, db: DB) -> dict:
    _require_delete()
    return plan_json(plan(db, body.ids, settings))


@router.post("/photos/delete")
def delete_photos(body: DeleteBody, db: DB):
    _require_delete()
    db.execute(select(func.pg_advisory_xact_lock(cast(LOCK_DELETE, Integer), cast(0, Integer))))
    fresh = plan(db, body.ids, settings)
    if fresh.moves != body.moves or (body.excluded is not None and fresh.excluded != body.excluded):
        db.rollback()
        return JSONResponse({**plan_json(fresh), "detail": CHANGED}, status_code=409)
    if not fresh.groups:
        db.rollback()
        raise HTTPException(404, "Nothing to delete: no such photo, or it is already being deleted")

    db.execute(
        update(Photo)
        .where(Photo.id.in_(fresh.photo_ids), Photo.trashed_at.is_(None))
        .values(trashed_at=utcnow())
    )
    job = Job(kind="delete", params={"photo_ids": fresh.photo_ids, "plan": fresh.to_json()})
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job": job_json(job), "count": len(fresh.photo_ids)}
