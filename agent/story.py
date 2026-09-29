"""Storyboards: a video's frames side by side, for scrubbing on hover.

The grid shows a video as one frame. Resting the pointer on it shows more:
the strip holds up to ``STORY_FRAMES`` frames spread over the clip, each
``STORY_HEIGHT`` pixels high, and the UI shows the one under the pointer. One
small WEBP per video, drawn once, so hovering costs a single image request
and no video decoding in the browser at all.

What it costs is decoding, and an iPhone's 4K HEVC is expensive to decode:
measured in the agent's image, about 0.4 s of CPU for a keyframe and 0.1 s
for any other frame. So there are two ways to get the frames, and the cheaper
one for the clip is taken:

* **keyframes**, for anything longer than a few seconds of 4K: ``-ss``
  before ``-i`` seeks in the file, and ``-noaccurate_seek -skip_frame
  nokey`` hands over the keyframe it lands on and decodes nothing else. Ten
  seeks, ten keyframes, whatever the clip's length: a ten-minute 4K clip
  costs what a ten-second one does (about 3.5 s of CPU). The seeks are
  inputs of one ffmpeg (``hstack``). Ten ffmpegs side by side finish sooner
  for one video, but spend twice the CPU, and the process pool already runs
  one video per core.
* **decode**, for clips short and small enough (``DECODE_BUDGET`` pixels in
  all): one pass through the clip, ``select`` keeping the frames nearest the
  wanted times. Exact frames, and as many as the clip has when that is fewer
  than ten.

Keyframes are only as far apart as the encoder chose, about a second on an
iPhone, so on a clip of a few seconds several seeks land on one keyframe.
Repeats are dropped: ``story_frames`` counts different frames only.

Runs in the process pool like the thumbnails: a top-level function taking one
picklable task, no settings, no database.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass

from PIL import Image

from core.media import STORY_FRAMES, STORY_HEIGHT

from . import video
from .thumbs import _write_webp

QUALITY = 75
# WEBP stops at 16,383 pixels a side. A strip of ten frames of a 32:9 video
# is 6,400 wide; wider than this per frame is a panorama recorded as video,
# squeezed rather than refused.
MAX_FRAME_WIDTH = 1600
# Pixels a "decode" pass may go through: 30 frames of 4K, 120 of 1080p, 270
# of 720p. Past it, ten keyframes are cheaper.
DECODE_BUDGET = 250_000_000
# Keyframes are at least this far apart (seconds): fewer seeks than that on a
# short clip, because the extra ones could only land on a keyframe again.
KEYFRAME_GAP = 0.5
TIMEOUT = 180.0


class StoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoryTask:
    photo_id: int
    src: str
    dest: str
    duration: float | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    hevc: bool = True           # this ffmpeg decodes HEVC
    tonemap: bool = True        # this ffmpeg has zscale + tonemap
    frames: int = STORY_FRAMES
    height: int = STORY_HEIGHT


@dataclass(frozen=True)
class Plan:
    times: tuple[float, ...]    # seconds from the start, one per wanted frame
    keyframes: bool             # seek to keyframes, or decode the clip once


def frame_width(width: int, height: int, frame_height: int = STORY_HEIGHT) -> int:
    """One frame's width for a video displayed ``width`` x ``height``: even,
    because zscale refuses odd sides (see ``thumbs._frame``)."""
    w = round(frame_height * width / height / 2) * 2
    return max(2, min(MAX_FRAME_WIDTH, w))


def _ratio(text) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", str(text or ""))
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return num / den if num > 0 and den > 0 else None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def video_stream(data: dict) -> dict:
    """The stream ``video.stream_info`` describes: the first video that is
    not cover art."""
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    raise StoryError("no video stream")


def frame_rate(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = _ratio(stream.get(key))
        # ffprobe writes 90000/1 and the like for streams it could not
        # measure; nobody records at more than a thousand frames a second.
        if rate and rate <= 1000:
            return rate
    return None


def frame_count(stream: dict, duration: float | None, fps: float | None) -> int | None:
    raw = stream.get("nb_frames")
    if raw is not None and str(raw).isdigit() and int(raw) > 0:
        return int(raw)
    if duration and fps:
        return max(1, round(duration * fps))
    return None


def plan(duration: float | None, count: int | None, pixels: int,
         frames: int = STORY_FRAMES) -> Plan:
    """Where the frames come from, for a clip of ``count`` frames of
    ``pixels`` each."""
    if not duration or duration <= 0:
        return Plan((0.0,), False)
    if count and count * pixels <= DECODE_BUDGET:
        n = max(1, min(frames, count))
        return Plan(tuple(duration * (i + 0.5) / n for i in range(n)), False)
    n = max(1, min(frames, math.ceil(duration / KEYFRAME_GAP)))
    return Plan(tuple(duration * (i + 0.5) / n for i in range(n)), True)


def _chain(task: StoryTask, width: int, hdr: bool) -> str:
    chain = f"scale={width}:{task.height},setsar=1"
    if hdr and task.tonemap:
        chain += "," + video.TONEMAP
    return chain + ",format=rgb24"


def argv(task: StoryTask, p: Plan, width: int, *, hdr: bool, stream: int = 0,
         fps: float | None = None) -> list[str]:
    """One ffmpeg; raw RGB frames on stdout, side by side (keyframes) or one
    after another (decode)."""
    cmd = [task.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    n = len(p.times)
    if p.keyframes:
        for at in p.times:
            # One decoder thread per input: they take turns anyway (ffmpeg
            # feeds the filter graph one input at a time), and ten
            # frame-threaded 4K decoders would hold gigabytes of frames.
            cmd += ["-threads", "1", "-skip_frame", "nokey", "-noaccurate_seek"]
            if at > 0:
                cmd += ["-ss", f"{at:.3f}"]
            cmd += ["-i", task.src]
        each = f"trim=end_frame=1,setpts=PTS-STARTPTS,{_chain(task, width, hdr)}"
        if n == 1:
            graph = f"[0:{stream}]{each}"
        else:
            parts = [f"[{i}:{stream}]{each}[f{i}]" for i in range(n)]
            graph = ";".join(parts) + ";" + "".join(f"[f{i}]" for i in range(n)) + f"hstack=inputs={n}"
        return cmd + ["-filter_complex", graph, "-frames:v", "1", "-an", "-sn", "-dn",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    # The first frame at or after each wanted time, half a frame early so a
    # clip of exactly n frames gets every one of them.
    step = (p.times[1] - p.times[0]) if n > 1 else 0.0
    early = 0.5 / fps if fps else 0.0
    first = max(0.0, p.times[0] - early)
    pick = f"select='gte(t-start_t\\,{first:.6f}+selected_n*{step:.6f})'"
    return cmd + ["-threads", "2", "-i", task.src, "-map", f"0:{stream}",
                  "-vf", f"{pick},{_chain(task, width, hdr)}", "-fps_mode", "passthrough",
                  "-frames:v", str(n), "-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def _run(cmd: list[str], ffmpeg: str) -> tuple[bytes, str]:
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=TIMEOUT, check=False)
    except FileNotFoundError as exc:
        raise StoryError(f"ffmpeg not found: {ffmpeg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StoryError("ffmpeg timed out drawing the storyboard") from exc
    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    reason = err[-1] if err else ("" if proc.returncode == 0 else f"exit {proc.returncode}")
    return (proc.stdout if proc.returncode == 0 else b""), reason


def frames_of(raw: bytes, width: int, height: int, *, side_by_side: bool) -> list[Image.Image]:
    """Raw RGB from ffmpeg as frames: one strip ``n`` frames wide, or ``n``
    frames one after another."""
    size = width * height * 3
    count = len(raw) // size
    if not count:
        return []
    if side_by_side:
        strip = Image.frombytes("RGB", (width * count, height), raw[:size * count])
        return [strip.crop((i * width, 0, (i + 1) * width, height)) for i in range(count)]
    return [Image.frombytes("RGB", (width, height), raw[i * size:(i + 1) * size]) for i in range(count)]


def distinct(frames: list[Image.Image]) -> list[Image.Image]:
    """Without the repeats: two seeks that landed on one keyframe decode to
    the very same pixels."""
    out: list[Image.Image] = []
    last = None
    for frame in frames:
        data = frame.tobytes()
        if data != last:
            out.append(frame)
        last = data
    return out


def grab(task: StoryTask) -> list[Image.Image]:
    """Up to ``task.frames`` different frames spread over the clip, each
    ``task.height`` pixels high. The text stage reads the same frames, only
    larger (agent/ocr.py)."""
    data = video.probe(task.src, task.ffprobe)
    info = video.stream_info(data)
    if info.codec == "hevc" and not task.hevc:
        raise StoryError(video.HEVC_MISSING)
    stream = video_stream(data)
    duration = task.duration or info.duration
    fps = frame_rate(stream)
    index = int(stream.get("index") or 0)
    p = plan(duration, frame_count(stream, duration, fps), info.width * info.height, task.frames)
    width = frame_width(info.width, info.height, task.height)

    raw, reason = _run(argv(task, p, width, hdr=info.hdr, stream=index, fps=fps), task.ffmpeg)
    frames = frames_of(raw, width, task.height, side_by_side=p.keyframes)
    if not frames and not p.keyframes:
        # A clip whose timestamps are not what the container promised: a
        # keyframe is always there to seek to.
        p = Plan(p.times, True)
        raw, reason = _run(argv(task, p, width, hdr=info.hdr, stream=index, fps=fps), task.ffmpeg)
        frames = frames_of(raw, width, task.height, side_by_side=True)
    if not frames:
        raise StoryError(f"ffmpeg: {reason}" if reason else "ffmpeg produced no frames")
    return distinct(frames) if p.keyframes else frames


def render(task: StoryTask) -> int:
    """Draw ``task.dest``; return how many frames it holds.

    The process pool's entry point: top-level, one picklable argument.
    """
    frames = grab(task)
    width = frames[0].width
    strip = Image.new("RGB", (width * len(frames), task.height))
    for i, frame in enumerate(frames):
        strip.paste(frame, (i * width, 0))
    _write_webp(strip, task.dest, quality=QUALITY)
    return len(frames)
