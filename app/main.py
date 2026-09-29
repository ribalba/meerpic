"""meerpic-server: the web layer.

It reads the database, draws the odd image the agent has not got to yet, and
enqueues what the user asks for. It never runs rclone, holds no iCloud
session, and writes nothing into the library: the pictures are mounted
read-only into its container. That split is meerail's and meercal's, and it is
here for the same reason: the process that talks to your iCloud account should
be the one on your own machine, not the one behind a web server.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core import embed
from core.config import get_settings
from core.database import init_db
from core.version import VERSION

from .routers import (
    albums,
    auth,
    delete,
    jobs,
    media,
    nsfw,
    photos,
    places,
    state,
    version,
)
from .security import PLAINTEXT_REFUSAL, is_secure_request, require_secure

# Uvicorn's own logger, so that these lines land in `docker compose logs`
# beside its startup banner instead of in a logger nobody configured.
log = logging.getLogger("uvicorn.error")

settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"

# The shell names every script it loads, so a reused copy of it pins the whole
# app to the version before this one. It is one small file from a local server.
SHELL_HEADERS = {"cache-control": "no-cache"}

# Paths that are never the single-page app: a missing thumbnail must be a 404
# an <img> can show as broken, not the app's HTML, and the same goes for a
# script that is not there.
NOT_THE_APP = ("/api/", "/media/", "/static/")


def _warm_search_model() -> threading.Thread:
    """Load the search model's text half in the background.

    The first search would otherwise pay for it: several seconds from the
    disk, half a minute and a 1.5 GB download the very first time. Not in the
    lifespan itself, because the grid must come up at once whether or not the
    model ever does; a failed download is logged, and search says so when it
    is used (see app/query.py), while everything else works.
    """

    def run() -> None:
        started = time.monotonic()
        try:
            embed.warm_text()
        # SystemExit too: an unknown model name is reported that way by
        # core/embed.py, and in a thread it would vanish without a word.
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            log.warning("meerpic: the search model did not load: %s: %s", type(exc).__name__, exc)
            return
        if not embed.FAKE:
            log.info("meerpic: search model %s ready in %.1f s",
                     embed.model_name(), time.monotonic() - started)

    thread = threading.Thread(target=run, name="meerpic-warm-search", daemon=True)
    thread.start()
    return thread


async def lifespan(_app: FastAPI):
    # Waits for Postgres and creates what is missing. Compose starts the two
    # together and `depends_on` only waits for the container.
    init_db()
    _warm_search_model()
    yield


app = FastAPI(
    title="meerpic",
    version=VERSION,
    description="The meerpic photo library: every photo you took, found fast.",
    lifespan=lifespan,
)

if settings.trusted_proxies:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

for module in (auth, version, state, delete, nsfw, photos, places, albums, jobs, media):
    app.include_router(module.router)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness for the container. Deliberately outside the password gate and
    deliberately not touching the database: it answers "is this process up",
    and a health check that fails because Postgres is restarting takes the web
    layer down with it for no reason."""
    return {"ok": True, "version": VERSION}


class RevalidatingStatic(StaticFiles):
    """Static files a browser must ask about before reusing.

    The script tags in index.html are unversioned, so a cached ``app.*.js`` is
    indistinguishable from the current one until something asks. Starlette
    already sends an ETag, but with no ``Cache-Control`` that is only an offer:
    a browser may reuse a file for as long as its own heuristic likes, which is
    how a fix can sit deployed on the server while the page in front of you
    keeps running the version before it.

    ``no-cache`` does not mean do not store. It means revalidate first, so the
    usual answer is a 304 with no body. meercal's, unchanged.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", "no-cache")
        return response


# check_dir=False: the UI lives in its own directory with its own owner, and a
# checkout without it should still start and answer the API.
app.mount("/static", RevalidatingStatic(directory=STATIC_DIR, check_dir=False), name="static")


def _shell() -> Response:
    # Read at request time from the module's STATIC_DIR, so a test can point
    # it at a page of its own.
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return JSONResponse({"detail": "The web UI is missing (app/static/index.html)"}, status_code=404)
    return FileResponse(index, headers=SHELL_HEADERS)


@app.get("/")
def index(request: Request) -> Response:
    # The connection is checked, the session is not: the shell holds no photo
    # and its whole job on a password-protected install is to put the login
    # form on screen. See app/security.py.
    require_secure(request)
    return _shell()


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> Response:
    # Anything under /api, /media or /static that does not exist is an error
    # with a reason; anything else is a deep link into the single-page app
    # and gets the app.
    path = request.url.path
    if path.startswith(NOT_THE_APP) or path in ("/api", "/media"):
        detail = exc.detail if isinstance(exc, StarletteHTTPException) else "Not found"
        return JSONResponse({"detail": detail}, status_code=404)
    # A deep link is the same shell by another name, and it is refused over
    # plaintext for the same reason. An exception handler cannot raise its way
    # out, so the refusal is written rather than thrown.
    if settings.server_password and not is_secure_request(request):
        return JSONResponse({"detail": PLAINTEXT_REFUSAL}, status_code=403)
    return _shell()
