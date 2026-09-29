"""Browser-playable copies of videos: what to run, decided from ffprobe.

iPhones record HEVC, and no browser on Linux plays it, so every video gets a
preview: H.264 High, yuv420p, AAC (or silence), ``+faststart`` so playback
starts before the download ends, and no taller than ``video.height`` on its
short edge. A source that already is all of that is remuxed with ``-c copy``,
which takes a moment; everything else is transcoded.

HDR matters more than it looks. An iPhone since the 12 records HLG (and PQ in
some modes) in 10-bit HEVC; converted naively to 8-bit BT.709 it comes out
grey and washed, and every video from a recent phone looks broken. With
zscale (zimg) the agent tone-maps to SDR properly; without it, it says so in
the log and makes the washed copy rather than none.

The ffmpeg this runs against is not always the one it was written for:
Fedora's ``ffmpeg-free`` has no HEVC decoder and no libx264. ``capabilities``
reads what the binary has, the plan uses what is there (libopenh264 for
H.264 when libx264 is missing, so a development run still works on H.264
sources), and an HEVC source on such a host fails with a sentence that says
what to do instead of ffmpeg's "Decoder not found".

Nothing in here touches the database; ``agent/previews.py`` runs the plans.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

HEVC_MISSING = "this ffmpeg cannot decode HEVC; run the agent in its container"

# ffmpeg's colour_transfer names for the two HDR curves phones record.
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

# The tone-mapping chain from the spec: linearise, map to BT.709 primaries,
# compress with Hable, then back to a BT.709 transfer in limited range.
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)

# Decoders and encoders that exist in the list but need hardware (and a
# device node) this container does not have. Counting them would promise an
# HEVC decoder that fails at the first frame.
_HW_SUFFIXES = ("_qsv", "_cuvid", "_v4l2m2m", "_vaapi", "_vdpau", "_mediacodec",
                "_mmal", "_rkmpp", "_amf", "_nvenc", "_videotoolbox", "_vulkan", "_d3d11va")


class VideoError(RuntimeError):
    """A video this ffmpeg cannot turn into a preview, with the reason."""


@dataclass(frozen=True)
class Caps:
    """What one ffmpeg binary can do, as far as previews care."""

    found: bool = False
    decoders: frozenset[str] = field(default_factory=frozenset)   # codec names
    encoders: frozenset[str] = field(default_factory=frozenset)   # encoder names
    filters: frozenset[str] = field(default_factory=frozenset)

    @property
    def hevc(self) -> bool:
        return "hevc" in self.decoders

    @property
    def h264_encoder(self) -> str | None:
        for name in ("libx264", "libopenh264"):
            if name in self.encoders:
                return name
        return None

    @property
    def tonemap(self) -> bool:
        return {"zscale", "tonemap"} <= self.filters

    def can_decode(self, codec: str) -> bool:
        return codec in self.decoders


_LIST_LINE = re.compile(r"^\s*([A-Z.]{3,6})\s+(\S+)\s*(.*)$")
_CODEC_OF = re.compile(r"\(codec (\S+)\)")


def parse_codec_list(text: str) -> dict[str, str]:
    """``ffmpeg -encoders`` / ``-decoders`` output as {name: codec}.

    A line is ``" V....D libopenh264   OpenH264 ... (codec h264)"``: the codec
    is the one in brackets, or the name itself when there are none. The
    legend at the top (``" V..... = Video"``) is skipped.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LIST_LINE.match(line)
        if not m or m.group(2) == "=" or set(m.group(1)) == {"-"}:
            continue
        name, rest = m.group(2), m.group(3)
        codec = _CODEC_OF.search(rest)
        out[name] = codec.group(1) if codec else name
    return out


def parse_filter_list(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        m = re.match(r"^\s*([TSC.|]{3})\s+(\S+)\s+\S+->\S+", line)
        if m:
            out.add(m.group(2))
    return out


def _run_list(ffmpeg: str, flag: str) -> str:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", flag],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
    )
    return proc.stdout.decode("utf-8", "replace")


@lru_cache(maxsize=8)
def capabilities(ffmpeg: str = "ffmpeg") -> Caps:
    """What ``ffmpeg`` can do. Asked once per process and binary."""
    try:
        decoders = parse_codec_list(_run_list(ffmpeg, "-decoders"))
        encoders = parse_codec_list(_run_list(ffmpeg, "-encoders"))
        filters = parse_filter_list(_run_list(ffmpeg, "-filters"))
    except (OSError, subprocess.SubprocessError):
        return Caps()
    soft = {name: codec for name, codec in decoders.items() if not name.endswith(_HW_SUFFIXES)}
    return Caps(
        found=True,
        decoders=frozenset(soft.values()),
        encoders=frozenset(n for n in encoders if not n.endswith(_HW_SUFFIXES)),
        filters=frozenset(filters),
    )


# --- probing --------------------------------------------------------------------


def probe(path: str, ffprobe: str = "ffprobe", timeout: float = 60.0) -> dict:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise VideoError(f"ffprobe not found: {ffprobe}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoError("ffprobe timed out") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VideoError(f"ffprobe: {err[-1] if err else 'exit ' + str(proc.returncode)}")
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise VideoError(f"ffprobe output is not JSON: {exc}") from exc


def _float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class StreamInfo:
    codec: str
    width: int              # as displayed: rotation and pixel aspect applied
    height: int
    pix_fmt: str
    bits: int
    transfer: str
    rotation: int           # degrees, 0/90/180/270
    duration: float | None
    audio: str | None       # the first audio stream's codec, or None

    @property
    def hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return round(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int((stream.get("tags") or {}).get("rotate", 0)) % 360
    except (TypeError, ValueError):
        return 0


def _bits(stream: dict) -> int:
    raw = stream.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    pix = stream.get("pix_fmt") or ""
    m = re.search(r"p(\d{2})(?:le|be)$", pix)
    return int(m.group(1)) if m else 8


def stream_info(data: dict) -> StreamInfo:
    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise VideoError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if w <= 0 or h <= 0:
        raise VideoError("video stream has no size")
    # Non-square pixels (DV, old MPEG-2): the displayed width is the stored
    # one times the sample aspect ratio.
    sar = str(video.get("sample_aspect_ratio") or "")
    m = re.match(r"^(\d+):(\d+)$", sar)
    if m and int(m.group(1)) > 0 and int(m.group(2)) > 0 and m.group(1) != m.group(2):
        w = max(2, round(w * int(m.group(1)) / int(m.group(2))))
    rotation = _rotation(video)
    if rotation in (90, 270):
        w, h = h, w
    duration = _float(video.get("duration")) or _float((data.get("format") or {}).get("duration"))
    return StreamInfo(
        codec=str(video.get("codec_name") or ""),
        width=w,
        height=h,
        pix_fmt=str(video.get("pix_fmt") or ""),
        bits=_bits(video),
        transfer=str(video.get("color_transfer") or ""),
        rotation=rotation,
        duration=duration,
        audio=str(audio.get("codec_name") or "") if audio else None,
    )


# --- the plan -------------------------------------------------------------------


def _even(x: float) -> int:
    return max(2, round(x / 2.0) * 2)


def preview_size(width: int, height: int, limit: int) -> tuple[int, int]:
    """Displayed size scaled so the short edge is at most ``limit``, even."""
    scale = min(1.0, limit / min(width, height))
    return _even(width * scale), _even(height * scale)


@dataclass(frozen=True)
class Plan:
    mode: str                   # "remux" | "transcode"
    args: list[str]             # everything between the input and the output
    encoder: str = ""
    tonemapped: bool = False
    note: str = ""              # one line for the log when something was compromised


def plan(
    info: StreamInfo,
    caps: Caps,
    *,
    height: int = 720,
    crf: int = 23,
    preset: str = "veryfast",
) -> Plan:
    """How to turn this source into a preview with this ffmpeg."""
    short = min(info.width, info.height)
    if (
        info.codec == "h264"
        and info.pix_fmt in ("yuv420p", "yuvj420p")
        and info.bits == 8
        and not info.hdr
        and short <= height
        and info.audio in (None, "aac")
    ):
        return Plan("remux", [
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-movflags", "+faststart",
        ])

    if not caps.can_decode(info.codec):
        if info.codec == "hevc":
            raise VideoError(HEVC_MISSING)
        raise VideoError(f"this ffmpeg cannot decode {info.codec or 'this video'}")
    encoder = caps.h264_encoder
    if encoder is None:
        raise VideoError("this ffmpeg has no H.264 encoder (libx264); run the agent in its container")

    notes = []
    tw, th = preview_size(info.width, info.height, height)
    # Scale before tone-mapping: the same picture for a fraction of the
    # float32 work zscale does per pixel.
    chain = [f"scale={tw}:{th}"]
    tonemapped = False
    if info.hdr:
        if caps.tonemap:
            chain.append(TONEMAP)
            tonemapped = True
        else:
            notes.append("HDR source but no zscale/tonemap in this ffmpeg: colours will look washed")
    if not tonemapped:
        chain.append("format=yuv420p")

    if encoder == "libx264":
        venc = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    else:
        # openh264 has neither presets nor CRF. A bitrate by pixel count keeps
        # a 720p preview at roughly what libx264 at CRF 23 makes of it.
        rate = max(800_000, int(tw * th * 3))
        venc = ["-c:v", encoder, "-b:v", str(rate), "-pix_fmt", "yuv420p"]
        notes.append("libx264 missing, using libopenh264 (development fallback)")

    args = [
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", ",".join(chain),
        # Slow-motion clips are 120 or 240 fps; a browser preview does not
        # need more than 60 and some decoders refuse more.
        "-fpsmax", "60",
        *venc,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
    ]
    return Plan("transcode", args, encoder=encoder, tonemapped=tonemapped, note="; ".join(notes))


def argv(ffmpeg: str, src: str, dest_tmp: str, p: Plan, *, nice: str | None = "nice") -> list[str]:
    """The full command line, at low priority so the desktop stays usable."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, *p.args, "-f", "mp4", dest_tmp]
    return [nice, "-n", "10", *cmd] if nice else cmd


def timeout_for(info: StreamInfo | None, mode: str) -> float:
    """Seconds before a preview is given up on. Generous: a slow machine
    under load is fine, a hung process is not."""
    duration = info.duration if info and info.duration else None
    if duration is None:
        return 1800.0
    if mode == "remux":
        return 60.0 + duration
    return 120.0 + duration * 20.0
