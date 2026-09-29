"""``--test``: prove every dependency once, change nothing, say what is wrong.

The agent leans on five programs, two models and three places (exiftool,
ffmpeg, ffprobe, rclone; the search model, the NSFW classifier; the library,
the cache, the database),
and each one failing shows up much later as a grey tile or a video that will
not play. This runs each check once, prints one line per check, and exits 1
if any failed, so ``make test-agent`` or a container healthcheck can say so
before the first photo is touched.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable

from core.config import Settings

from . import video

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          check=False)


def check_exiftool(settings: Settings) -> tuple[str, str]:
    try:
        proc = _run([settings.exiftool, "-ver"])
    except (OSError, subprocess.SubprocessError) as exc:
        return FAIL, f"not runnable ({settings.exiftool}): {exc}"
    if proc.returncode != 0:
        return FAIL, f"exit {proc.returncode}"
    return OK, f"version {proc.stdout.decode().strip()}"


def check_ffmpeg(settings: Settings) -> list[tuple[str, str, str]]:
    caps = video.capabilities(settings.ffmpeg)
    if not caps.found:
        return [("ffmpeg", FAIL, f"not runnable ({settings.ffmpeg})")]
    out = [("ffmpeg", OK, settings.ffmpeg)]
    out.append(("ffmpeg HEVC decoder", OK, "hevc") if caps.hevc else
               ("ffmpeg HEVC decoder", FAIL, "missing: iPhone videos cannot be previewed"))
    if "libx264" in caps.encoders:
        out.append(("ffmpeg libx264", OK, "libx264"))
    elif caps.h264_encoder:
        out.append(("ffmpeg libx264", FAIL, f"missing (falls back to {caps.h264_encoder}, development only)"))
    else:
        out.append(("ffmpeg libx264", FAIL, "missing: no H.264 encoder at all"))
    out.append(("ffmpeg HDR tone-mapping", OK, "zscale + tonemap") if caps.tonemap else
               ("ffmpeg HDR tone-mapping", WARN, "zscale/tonemap missing: HDR videos look washed"))
    return out


def check_ffprobe(settings: Settings) -> tuple[str, str]:
    try:
        proc = _run([settings.ffprobe, "-version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return FAIL, f"not runnable ({settings.ffprobe}): {exc}"
    first = proc.stdout.decode("utf-8", "replace").splitlines()[:1]
    return (OK, first[0]) if proc.returncode == 0 and first else (FAIL, f"exit {proc.returncode}")


def check_rclone(settings: Settings) -> tuple[str, str]:
    if not settings.sync_enabled:
        return OK, "sync disabled, not needed"
    remote = settings.sync_remote.split(":", 1)[0] + ":"
    try:
        proc = _run([settings.rclone, "listremotes"])
    except (OSError, subprocess.SubprocessError) as exc:
        return FAIL, f"not runnable ({settings.rclone}): {exc}"
    remotes = proc.stdout.decode("utf-8", "replace").split()
    if remote not in remotes:
        have = ", ".join(remotes) or "none"
        return FAIL, f"remote {remote} is not configured (have: {have}); run: rclone config"
    return OK, f"remote {remote} configured"


def check_root(path) -> tuple[str, str]:
    if not path.is_dir():
        return FAIL, f"{path} does not exist or is not a folder"
    if not os.access(path, os.R_OK | os.X_OK):
        return FAIL, f"{path} is not readable"
    try:
        with os.scandir(path) as it:
            n = sum(1 for _ in zip(it, range(100_000)))
    except OSError as exc:
        return FAIL, f"{path}: {exc}"
    return OK, f"{path} ({n} entries)"


def check_cache(settings: Settings) -> tuple[str, str]:
    path = settings.cache_path
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".write-test-"):
            pass
    except OSError as exc:
        return FAIL, f"{path} is not writable: {exc}"
    return OK, str(path)


def check_db() -> tuple[str, str]:
    from sqlalchemy import text

    from core.database import engine

    try:
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
            has_vector = conn.execute(
                text("SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'")
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - any failure is the verdict
        return FAIL, f"{type(exc).__name__}: {exc}".splitlines()[0]
    if not has_vector:
        return FAIL, f"PostgreSQL {version} without the pgvector extension"
    return OK, f"PostgreSQL {version} with pgvector"


def check_model() -> tuple[str, str]:
    from PIL import Image

    from core import embed

    try:
        if not embed.cache_ready():
            note = " (first use: downloading the model)"
        else:
            note = ""
        vec = embed.embed_images([Image.new("RGB", (64, 64), (120, 160, 200))])[0]
    except Exception as exc:  # noqa: BLE001 - any failure is the verdict
        return FAIL, f"{embed.model_name()}: {type(exc).__name__}: {exc}".splitlines()[0]
    return OK, f"{embed.model_name()}, {len(vec)} dimensions{note}"


def check_nsfw(settings: Settings) -> tuple[str, str]:
    from PIL import Image

    from . import nsfw

    if not settings.nsfw_enabled:
        return OK, "disabled (nsfw.enabled = false)"
    try:
        score = nsfw.model(settings).score([Image.new("RGB", (64, 64), (120, 160, 200))])[0]
    except Exception as exc:  # noqa: BLE001 - any failure is the verdict
        return FAIL, f"{nsfw.model_name(settings)}: {type(exc).__name__}: {exc}".splitlines()[0]
    return OK, f"{nsfw.model_name(settings)} (a grey test image scores {score:.3f})"


def check_faces(settings: Settings) -> tuple[str, str]:
    from PIL import Image

    from . import faces

    if not settings.faces_enabled:
        return OK, "disabled (faces.enabled = false)"
    try:
        models = faces.model(settings)
        # A grey square has no face in it: what is checked is that both
        # models load and run.
        grey = Image.new("RGB", (faces.ALIGN_EDGE, faces.ALIGN_EDGE), (128, 128, 128))
        found = faces.find(models, faces.source_of(grey), settings.faces_detect_score)
        models.embed([grey])
    except Exception as exc:  # noqa: BLE001 - any failure is the verdict
        return FAIL, f"{faces.model_name(settings)}: {type(exc).__name__}: {exc}".splitlines()[0]
    return OK, f"{faces.model_name(settings)} ({len(found)} face(s) in a grey test image)"


def check_ocr(settings: Settings) -> tuple[str, str]:
    from PIL import Image, ImageDraw, ImageFont

    from . import ocr

    if not settings.ocr_enabled:
        return OK, "disabled (ocr.enabled = false)"
    # ASCII: Pillow's built-in font draws a box for an umlaut.
    img = Image.new("RGB", (480, 120), (255, 255, 255))
    ImageDraw.Draw(img).text((24, 30), "Rechnung 2026", fill=(0, 0, 0), font=ImageFont.load_default(48))
    try:
        lines = ocr.engine(ocr.models(settings)).read(img)
    except Exception as exc:  # noqa: BLE001 - any failure is the verdict
        return FAIL, f"{ocr.model_name()}: {type(exc).__name__}: {exc}".splitlines()[0]
    said = " / ".join(line.text for line in lines) or "nothing"
    return OK, f"{ocr.model_name()} (a test image with \"Rechnung 2026\" reads: {said})"


def run(settings: Settings, out: Callable[[str], None] = print) -> int:
    """Every check, one line each. Returns the number of failures."""
    results: list[tuple[str, str, str]] = []
    results.append(("exiftool", *check_exiftool(settings)))
    results += check_ffmpeg(settings)
    results.append(("ffprobe", *check_ffprobe(settings)))
    results.append(("rclone", *check_rclone(settings)))
    for name, path in settings.roots.items():
        results.append((f"library root {name}", *check_root(path)))
    results.append(("cache", *check_cache(settings)))
    results.append(("database", *check_db()))
    results.append(("search model", *check_model()))
    results.append(("nsfw model", *check_nsfw(settings)))
    results.append(("face models", *check_faces(settings)))
    results.append(("text models", *check_ocr(settings)))
    width = max(len(name) for name, _, _ in results)
    for name, verdict, detail in results:
        out(f"  {verdict:<4}  {name:<{width}}  {detail}")
    failures = sum(1 for _, verdict, _ in results if verdict == FAIL)
    out("")
    out("all checks passed" if not failures else f"{failures} check(s) failed")
    return failures
