"""What version this server is.

Behind the gate like everything else under /api, for meercal's reason: the
version of the software you run is exactly what an internet-wide scanner would
like to collect. /healthz stays open for the container's health check; this
does not. No update check for now: meercal's asks GitHub, and this app does
not talk to anything but the map's tile server.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.version import VERSION

from ..security import require_auth

router = APIRouter(prefix="/api", tags=["version"], dependencies=[Depends(require_auth)])


@router.get("/version")
def version_info() -> dict:
    return {"version": VERSION}
