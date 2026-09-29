"""Capture the website screenshots from the running demo stack.

Drives the real UI in Chromium, against the meerpic built from this checkout
and a library of CC0 demo photos (demo_photos.py), so a shot on the site is a
shot the app actually produces. Nothing is mocked and nothing is retouched.

    make -C website screenshots                     # the whole pipeline
    python website/screenshots/shoot.py              # every shot, stack already seeded
    python website/screenshots/shoot.py --only map viewer
    python website/screenshots/shoot.py --phone      # the demo library's two iPhone screenshots

Output: website/public/img/screenshots/<name>.webp at 2880x1800 (1440x900 at
2x device scale, shown at half that so they stay sharp on retina panels), and
grid-dark.webp for the page's dark-mode <picture>. The WebP is encoded by
Chromium itself, so this needs Playwright and nothing else.

Every capture checks that the thing it is photographing is on screen before
it spends a screenshot on it, which is what stops a broken demo from shipping
as a blank-looking image.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import sys
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import demo_photos as demo

URL = os.environ.get("MEERPIC_DEMO_URL", "http://127.0.0.1:" + os.environ.get("DEMO_PORT", "18040"))
OUT = HERE.parent / "public" / "img" / "screenshots"
SITE = HERE.parent / "public" / "index.html"

VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2
QUALITY = 0.86
# Long enough for a grid page to lay out and its thumbnails to decode.
SETTLE = 900
# Row height of the grid: one press of `-` from the default 200, which fits
# a day of photos on a row at this width. The setting lives on the server
# (/api/prefs), so it is written before every run rather than left to
# whatever the last run or a visitor to the demo stack pressed.
ROW_HEIGHT = 170


def set_prefs() -> None:
    body = json.dumps({"value": {"size": ROW_HEIGHT}}).encode()
    req = urllib.request.Request(f"{URL}/api/prefs", data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        fail(f"cannot reach the demo stack at {URL} ({exc}); make -C website demo-up")


def fail(msg: str) -> None:
    sys.exit(f"shoot: {msg}")


# --- helpers ---------------------------------------------------------------------


def to_webp(page, png: bytes) -> bytes:
    """PNG -> WebP, in the browser: no Pillow on the host."""
    data = page.evaluate(
        """async ([b64, q]) => {
            const img = new Image();
            img.src = 'data:image/png;base64,' + b64;
            await img.decode();
            const c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img, 0, 0);
            return c.toDataURL('image/webp', q);
        }""",
        [base64.b64encode(png).decode(), QUALITY],
    )
    return base64.b64decode(data.split(",", 1)[1])


def shot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png = page.screenshot()
    path = OUT / f"{name}.webp"
    path.write_bytes(to_webp(page, png))
    print(f"  {path.relative_to(HERE.parent.parent)} ({path.stat().st_size // 1024} KB)")


def open_app(page, **params) -> None:
    query = urlencode({k: v for k, v in params.items() if v})
    page.goto(f"{URL}/{'?' + query if query else ''}", wait_until="domcontentloaded")
    page.wait_for_selector("#nav-tree .nav-item, #nav-tree a, #nav-tree button", timeout=15000)
    page.evaluate("document.fonts.ready")


def settle_grid(page, at_least: int = 1) -> int:
    """Wait for tiles, and for every thumbnail in view to have real pixels."""
    page.wait_for_function(
        f"document.querySelectorAll('#grid-body .tile').length >= {at_least}", timeout=20000
    )
    page.wait_for_function(
        """() => [...document.querySelectorAll('#grid-body .tile img')]
                  .filter(i => { const r = i.getBoundingClientRect();
                                 return r.bottom > 0 && r.top < innerHeight; })
                  .every(i => i.complete && i.naturalWidth > 0)""",
        timeout=20000,
    )
    page.wait_for_timeout(SETTLE)
    return page.locator("#grid-body .tile").count()


def photo_id(page, key: str) -> int:
    """The database id of a demo item, found the way a user would: by file name."""
    rows = demo.by_key(demo.resolve())
    name = rows[key].edit or rows[key].rel_path
    name = name.rsplit("/", 1)[-1]
    got = page.evaluate(
        """async (name) => {
            const r = await fetch('/api/photos?' + new URLSearchParams({q: 'file:' + name}));
            const j = await r.json();
            const list = j.photos || j.items || j.cards || [];
            return list.length ? list[0].id : null;
        }""",
        name,
    )
    if not got:
        fail(f"no photo named {name} (demo item {key!r}); is the demo stack seeded?")
    return int(got)


# --- the shots -----------------------------------------------------------------------

SHOTS = {}


def register(fn):
    SHOTS[fn.__name__] = fn
    return fn


@register
def grid(page, suffix=""):
    """The timeline, newest first: the hero shot, in both themes."""
    open_app(page)
    n = settle_grid(page, 20)
    if n < 20:
        fail(f"grid: only {n} tiles")
    shot(page, "grid" + suffix)


@register
def search(page, suffix=""):
    """Words search what is in the pictures."""
    open_app(page, q="cows")
    n = settle_grid(page, 5)
    if n < 5:
        fail(f"search: only {n} results for cows")
    shot(page, "search" + suffix)


@register
def similar(page, suffix=""):
    """`s` on a photo: what `similar:<id>` finds for a path through the dunes."""
    open_app(page)
    settle_grid(page)
    pid = photo_id(page, "dune-path")
    open_app(page, q=f"similar:{pid}")
    n = settle_grid(page, 6)
    if n < 6:
        fail(f"similar: only {n} results")
    shot(page, "similar" + suffix)


@register
def map(page, suffix=""):
    """Every located photo, clustered, over OpenStreetMap."""
    open_app(page, view="map")
    page.wait_for_selector(".cl-icon", timeout=20000)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        """() => [...document.querySelectorAll('.leaflet-tile')].length > 0 &&
                 [...document.querySelectorAll('.leaflet-tile')].every(t => t.complete)""",
        timeout=30000,
    )
    page.wait_for_function(
        "() => [...document.querySelectorAll('.cl-icon img')].every(i => i.complete && i.naturalWidth > 0)",
        timeout=20000,
    )
    page.wait_for_timeout(1500)
    if page.locator(".cl-icon").count() < 3:
        fail("map: fewer than three clusters")
    shot(page, "map" + suffix)


@register
def viewer(page, suffix=""):
    """One photo from the camera folder, with its info panel."""
    open_app(page)
    settle_grid(page)
    pid = photo_id(page, "alfama-roofs")
    open_app(page, photo=str(pid))
    page.wait_for_selector("#viewer:not([hidden])", timeout=15000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#vw-stage img')].some(i => i.complete && i.naturalWidth > 0)",
        timeout=30000,
    )
    if page.locator("#vw-info[hidden]").count():
        page.keyboard.press("i")
    page.wait_for_selector("#vw-info:not([hidden]) .info-row", timeout=10000)
    page.wait_for_timeout(1500)
    shot(page, "viewer" + suffix)


@register
def text(page, suffix=""):
    """`text:wurst`: the sign on a Berlin sausage stand, read by the OCR stage.

    Opened from the search, so the viewer marks the word in what was read,
    and with the pointer on that line, so the photo outlines where it is.
    """
    open_app(page)
    settle_grid(page)
    pid = photo_id(page, "wurst")
    open_app(page, q="text:wurst", photo=str(pid))
    page.wait_for_selector("#viewer:not([hidden])", timeout=15000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#vw-stage img')].some(i => i.complete && i.naturalWidth > 0)",
        timeout=30000,
    )
    if page.locator("#vw-info[hidden]").count():
        page.keyboard.press("i")
    try:
        page.wait_for_selector("#vw-info .info-text mark", timeout=15000)
    except Exception:  # noqa: BLE001 - said in words instead of a timeout
        fail("text: nothing marked in the info panel; did the OCR stage read the WURST sign?")
    page.locator("#vw-info .info-text-line", has=page.locator("mark")).first.hover()
    page.wait_for_timeout(1200)
    shot(page, "text" + suffix)


@register
def album(page, suffix=""):
    """An album, as iCloud has it, from the sidebar."""
    open_app(page, q='album:"Baltic Sea"')
    n = settle_grid(page, 8)
    if n < 8:
        fail(f"album: only {n} photos in Baltic Sea; is the demo stack seeded?")
    shot(page, "album" + suffix)


@register
def delete(page, suffix=""):
    """Three photos selected and Delete pressed: the plan, file by file.

    Only the plan is asked for (POST /api/photos/delete/plan, read-only);
    the dialog is closed with Escape and nothing is confirmed.
    """
    open_app(page, q='album:"Baltic Sea"')
    settle_grid(page, 8)
    tiles = page.locator("#grid-body .tile")
    for i in (0, 1, 2):
        tiles.nth(i).hover()
        tiles.nth(i).locator(".tile-check").click()
    page.wait_for_selector("#selbar:not([hidden])", timeout=5000)
    page.keyboard.press("Delete")
    page.wait_for_selector(".confirm-card .confirm-cmds", timeout=15000)
    page.wait_for_timeout(800)
    shot(page, "delete" + suffix)
    page.keyboard.press("Escape")
    page.wait_for_selector(".confirm-card", state="detached", timeout=5000)


# --- the demo library's own screenshots -------------------------------------------------


def phone(browser) -> None:
    """Two iPhone screenshots of this website, for the demo library.

    A phone's library is full of screenshots, and `is:screenshot` wants some.
    These are of the page these very screenshots end up on, rendered at an
    iPhone 16 Pro's size (1206x2622), so they are nobody's but ours.
    """
    if not SITE.is_file():
        fail(f"no {SITE}; the site has to exist before its phone screenshots can")
    sources = Path(os.environ.get("DEMO_DIR", "")) / "sources"
    if not os.environ.get("DEMO_DIR"):
        fail("DEMO_DIR is not set (make -C website demo-phone sets it)")
    sources.mkdir(parents=True, exist_ok=True)
    # Over HTTP, not file://: the page's paths are absolute (/css/site.css).
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE.parent)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ctx = browser.new_context(viewport={"width": 402, "height": 874}, device_scale_factor=3,
                                  is_mobile=True, has_touch=True, color_scheme="light")
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/", wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(800)
        page.screenshot(path=str(sources / "screen-site.png"))
        page.evaluate("document.querySelector('#features').scrollIntoView()")
        page.wait_for_timeout(800)
        page.screenshot(path=str(sources / "screen-features.png"))
        ctx.close()
    finally:
        httpd.shutdown()
    print(f"  {sources / 'screen-site.png'}\n  {sources / 'screen-features.png'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=sorted(SHOTS), help="just these shots")
    ap.add_argument("--phone", action="store_true", help="only render the demo library's phone screenshots")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            if args.phone:
                phone(browser)
                return 0
            names = args.only or list(SHOTS)
            set_prefs()
            errors: list[str] = []
            for scheme in ("light", "dark"):
                todo = names if scheme == "light" else [n for n in names if n == "grid"]
                if not todo:
                    continue
                ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                          color_scheme=scheme, reduced_motion="reduce",
                                          locale="en-GB", timezone_id="Europe/Berlin")
                for name in todo:
                    print(f"{scheme}: {name}")
                    page = ctx.new_page()
                    page.on("pageerror", lambda e, n=name: errors.append(f"{n}: {e}"))
                    SHOTS[name](page, "-dark" if scheme == "dark" else "")
                    page.close()
                ctx.close()
            if errors:
                print("page errors:", *errors, sep="\n  ", file=sys.stderr)
                return 1
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
