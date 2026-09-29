"""Sign in, when there is anything to sign in to.

meercal's, unchanged apart from the name on the cookie (see app/security.py).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from core.config import get_settings

from .. import security

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


class Login(BaseModel):
    password: str = ""


@router.get("/state")
def state(request: Request) -> dict:
    """Whether a password is needed at all, asked before the app draws itself."""
    return {
        "required": bool(settings.server_password),
        "secure": security.is_secure_request(request),
    }


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    if not settings.server_password:
        return {"ok": True}
    if not security.is_secure_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Refusing to take a password over plain HTTP")
    if not security.password_ok(body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    response.set_cookie(
        security.COOKIE,
        security.issue_token(),
        max_age=security.MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(security.COOKIE)
    return {"ok": True}
