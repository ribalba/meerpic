"""The optional password gate.

A localhost install is open: there is nothing between the browser and the app
but the loopback interface, and a password there protects nothing that the
user's own login has not already protected. Setting ``server.password`` turns
the gate on, and with it the requirement that nothing at all is served over a
plaintext connection to anywhere but loopback, because a browser that gets the
page gets the login form, and by then the password has already crossed the
network whatever the server says afterwards.

The session cookie is a signed value rather than a database row: this app has
one user and one password, so a revocation list would be a table with nothing
in it. Changing ``secret_key`` logs every browser out, which is the revocation.

A cookie rather than only a bearer header, because most of what this app
serves is fetched by ``<img>`` and ``<video>`` tags, which cannot be told to
send a header. The same cookie opens ``/media`` and ``/api`` alike.

The shape and the reasoning are meercal's, on purpose: the two sit side by
side and a fix to one gate should read as a fix to the other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time

from fastapi import Cookie, Header, HTTPException, Request, status

from core.config import get_settings

settings = get_settings()

# Said by everything that refuses a plaintext connection, so that the answer to
# "why is my reverse proxy getting a 403" is in one place.
PLAINTEXT_REFUSAL = (
    "meerpic is password-protected, so it refuses to answer over plain HTTP. "
    "Use https, or reach it on loopback. If TLS ends at a reverse proxy in "
    "front of this, that proxy has to be named in server.trusted_proxies."
)

COOKIE = "meerpic_session"
# A month. Long, because the alternative to a long session on a personal
# photo library is a password typed so often it ends up in a password
# manager's autofill anyway, and the cookie is HttpOnly and Secure where it
# matters.
MAX_AGE = 30 * 24 * 3600

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _sign(payload: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue_token(now: float | None = None) -> str:
    issued = int(now or time.time())
    payload = f"{issued}"
    return f"{payload}.{_sign(payload)}"


def token_valid(token: str, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        issued = int(payload)
    except ValueError:
        return False
    return (now or time.time()) - issued < MAX_AGE


def password_ok(given: str) -> bool:
    # Constant-time: the comparison is over a secret, and a timing difference
    # here is a free oracle for anything that can reach the port.
    return bool(settings.server_password) and hmac.compare_digest(
        given or "", settings.server_password
    )


def trusts_proxy(host: str) -> bool:
    """Is `host` one of ``server.trusted_proxies``?

    The same three forms uvicorn's ProxyHeadersMiddleware accepts, because the
    two read the same setting and a disagreement between them is a deployment
    that is half-trusted in a way nobody can see: a literal address, a CIDR
    range, or ``*`` for any peer that can reach the port. Behind a PaaS the
    range is usually the only form that stays true: the proxy's address is
    Docker's to assign, and it changes when the container is recreated.
    """
    proxies = settings.trusted_proxies
    if not host or not proxies:
        return False
    if "*" in proxies or host in proxies:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name, and the literal match above was its only chance
    for entry in proxies:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def is_secure_request(request: Request) -> bool:
    """Would this request's password stay off the wire?"""
    host = request.client.host if request.client else ""
    if host in LOOPBACK:
        return True
    if request.url.scheme == "https":
        return True
    # Where TLS ended at a proxy, the scheme above is already "https": the
    # middleware in app/main.py rewrote it from this header, and it is added
    # exactly when there are trusted proxies to rewrite for. What is left for
    # this line is the chain: two proxies deep the header reads
    # "https,https", which that middleware does not recognise as a scheme and
    # therefore ignores.
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip() == "https" and trusts_proxy(host)


def require_secure(request: Request) -> None:
    """The transport half of the gate, without the session half.

    What the app shell is allowed to do before anyone has signed in. It carries
    nothing private (it is markup, script and stylesheet, the same bytes
    /static serves to anyone), and every photo arrives later through /api
    and /media, behind `require_auth`. What it must still not do is arrive
    over plaintext: the shell *is* the login form, and a login form that
    renders is a password about to be typed into the wire.

    So the front door checks the connection and leaves the session to the API.
    Gating it on the session too is a locked door with the key inside: the 401
    is JSON, the page that knows what to do with a 401 is the page being
    refused, and the browser shows the JSON.
    """
    if not settings.server_password:
        return
    if not is_secure_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, PLAINTEXT_REFUSAL)


def require_auth(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: a no-op unless a password is configured."""
    if not settings.server_password:
        return
    require_secure(request)
    if session and token_valid(session):
        return
    if authorization and authorization.lower().startswith("bearer ") and password_ok(authorization[7:].strip()):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def new_secret() -> str:
    return secrets.token_urlsafe(48)
