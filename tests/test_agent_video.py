"""Video previews: capability parsing, the remux/transcode decision, and real
ffmpeg runs where this machine's ffmpeg can do them.

The probe below is ffprobe's answer for a real Live Photo companion (HEVC,
stored 1920x1440 with a -90 degree display matrix, PCM audio), trimmed.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from agent_helpers import (
    make_video,
    needs_encoder,
    needs_ffmpeg,
    needs_h264_encoder,
    needs_tonemap,
)

from agent import video

IPHONE = {
    "streams": [
        {"index": 0, "codec_name": "hevc", "codec_type": "video", "width": 1920, "height": 1440,
         "pix_fmt": "yuv420p", "color_transfer": "bt709", "duration": "2.911667",
         "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
         "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_name": "pcm_s16le", "codec_type": "audio"},
    ],
    "format": {"duration": "2.911667"},
}

ENCODERS = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libopenh264          OpenH264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)
 V.S... mpeg4                MPEG-4 part 2
 A....D aac                  AAC (Advanced Audio Coding)
"""
DECODERS = """Decoders:
 V..... = Video
 ------
 V....D libopenh264          OpenH264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V..... hevc_qsv             HEVC video (Intel Quick Sync Video acceleration) (codec hevc)
 VF..BD mpeg4                MPEG-4 part 2
 A....D aac                  AAC (Advanced Audio Coding)
"""
FILTERS = """Filters:
  T.. = Timeline support
  | = Source or sink filter
 .S. tonemap           V->V       Conversion to/from different dynamic ranges.
 .SC zscale            V->V       Apply resizing, colorspace and bit depth conversion.
 TSC scale             V->V       Scale the input video size and/or convert the image format.
"""

FULL = video.Caps(found=True, decoders=frozenset({"hevc", "h264", "mpeg4"}),
                  encoders=frozenset({"libx264", "aac"}), filters=frozenset({"zscale", "tonemap"}))
FEDORA = video.Caps(found=True, decoders=frozenset({"h264", "mpeg4"}),
                    encoders=frozenset({"libopenh264", "aac", "mpeg4"}), filters=frozenset({"scale"}))


def info(**kw) -> video.StreamInfo:
    base = {"codec": "h264", "width": 1280, "height": 720, "pix_fmt": "yuv420p", "bits": 8,
            "transfer": "bt709", "rotation": 0, "duration": 10.0, "audio": "aac"}
    base.update(kw)
    return video.StreamInfo(**base)


# --- capabilities ----------------------------------------------------------------------


def test_parse_codec_list():
    enc = video.parse_codec_list(ENCODERS)
    assert enc == {"libopenh264": "h264", "h264_vaapi": "h264", "mpeg4": "mpeg4", "aac": "aac"}
    dec = video.parse_codec_list(DECODERS)
    assert dec["hevc_qsv"] == "hevc" and dec["libopenh264"] == "h264"


def test_parse_filter_list():
    assert video.parse_filter_list(FILTERS) == {"tonemap", "zscale", "scale"}


def test_capabilities_ignore_hardware_codecs(monkeypatch):
    text = {"-decoders": DECODERS, "-encoders": ENCODERS, "-filters": FILTERS}
    monkeypatch.setattr(video, "_run_list", lambda ffmpeg, flag: text[flag])
    video.capabilities.cache_clear()
    try:
        caps = video.capabilities("fake-ffmpeg")
    finally:
        video.capabilities.cache_clear()
    assert caps.found
    assert not caps.hevc                    # only hevc_qsv, which needs hardware
    assert caps.can_decode("h264")
    assert caps.h264_encoder == "libopenh264"
    assert "h264_vaapi" not in caps.encoders
    assert caps.tonemap


def test_capabilities_of_a_missing_binary():
    assert video.capabilities("/nonexistent/ffmpeg") == video.Caps()


def test_caps_prefers_libx264():
    both = video.Caps(found=True, encoders=frozenset({"libx264", "libopenh264"}))
    assert both.h264_encoder == "libx264"
    assert video.Caps(found=True).h264_encoder is None


# --- probing -------------------------------------------------------------------------------


def test_stream_info_of_an_iphone_companion():
    i = video.stream_info(IPHONE)
    assert (i.codec, i.width, i.height, i.rotation) == ("hevc", 1440, 1920, 270)
    assert i.audio == "pcm_s16le"
    assert i.duration == pytest.approx(2.9117, abs=1e-3)
    assert not i.hdr


def test_stream_info_hdr_bits_and_pixel_aspect():
    data = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
                         "pix_fmt": "yuv420p10le", "color_transfer": "arib-std-b67"}]}
    i = video.stream_info(data)
    assert i.hdr and i.bits == 10 and i.audio is None and i.duration is None
    dv = {"streams": [{"codec_type": "video", "codec_name": "mpeg2video", "width": 720, "height": 576,
                       "sample_aspect_ratio": "16:15", "tags": {"rotate": "90"}}]}
    i = video.stream_info(dv)
    assert (i.width, i.height) == (576, 768)


def test_stream_info_skips_cover_art_and_needs_a_video():
    data = {"streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 600,
         "disposition": {"attached_pic": 1}},
        {"codec_type": "audio", "codec_name": "aac"},
    ]}
    with pytest.raises(video.VideoError, match="no video stream"):
        video.stream_info(data)


# --- the plan --------------------------------------------------------------------------------


def test_h264_720p_with_aac_is_remuxed():
    p = video.plan(info(), FULL)
    assert p.mode == "remux"
    assert "-c" in p.args and p.args[p.args.index("-c") + 1] == "copy"
    assert "+faststart" in p.args


@pytest.mark.parametrize("change", [
    {"width": 1920, "height": 1080},        # short edge over the limit
    {"audio": "pcm_s16le"},                 # Live Photo audio
    {"pix_fmt": "yuv422p"},
    {"bits": 10, "pix_fmt": "yuv420p10le"},
    {"codec": "hevc"},
    {"transfer": "arib-std-b67"},
])
def test_anything_else_is_transcoded(change):
    p = video.plan(info(**change), FULL)
    assert p.mode == "transcode"
    assert p.encoder == "libx264"
    args = " ".join(p.args)
    assert "-preset veryfast -crf 23 -profile:v high -pix_fmt yuv420p" in args
    assert "-map 0:v:0 -map 0:a:0?" in args
    assert "-c:a aac -b:a 128k" in args


def test_portrait_is_scaled_on_its_short_edge_to_even_sizes():
    p = video.plan(info(codec="hevc", width=1081, height=1921), FULL, height=720)
    vf = p.args[p.args.index("-vf") + 1]
    assert vf.startswith("scale=720:1280,")
    assert video.preview_size(1440, 1920, 720) == (720, 960)
    assert video.preview_size(642, 362, 720) == (642, 362)
    assert video.preview_size(3841, 2161, 720) == (1280, 720)


def test_hdr_is_tone_mapped_when_zscale_exists():
    p = video.plan(info(codec="hevc", bits=10, pix_fmt="yuv420p10le", transfer="arib-std-b67",
                        width=3840, height=2160), FULL)
    vf = p.args[p.args.index("-vf") + 1]
    assert vf == "scale=1280:720," + video.TONEMAP
    assert p.tonemapped and not p.note


def test_hdr_without_zscale_warns_and_goes_on():
    p = video.plan(info(codec="h264", transfer="smpte2084", width=1920, height=1080), FEDORA)
    assert not p.tonemapped
    assert "washed" in p.note
    assert p.args[p.args.index("-vf") + 1].endswith("format=yuv420p")


def test_hevc_on_a_host_without_a_decoder():
    with pytest.raises(video.VideoError) as exc:
        video.plan(video.stream_info(IPHONE), FEDORA)
    assert str(exc.value) == "this ffmpeg cannot decode HEVC; run the agent in its container"


def test_openh264_fallback_when_libx264_is_missing():
    p = video.plan(info(width=1920, height=1080), FEDORA)
    assert p.encoder == "libopenh264"
    assert "-crf" not in p.args and "-b:v" in p.args
    assert "libopenh264" in p.note


def test_no_h264_encoder_at_all():
    caps = video.Caps(found=True, decoders=frozenset({"h264"}), encoders=frozenset({"aac"}))
    with pytest.raises(video.VideoError, match="no H.264 encoder"):
        video.plan(info(width=1920, height=1080), caps)


def test_unknown_codec():
    with pytest.raises(video.VideoError, match="cannot decode prores"):
        video.plan(info(codec="prores"), FULL)


def test_argv_and_timeouts():
    p = video.plan(info(), FULL)
    cmd = video.argv("ffmpeg", "/in.mov", "/out.tmp", p)
    assert cmd[:3] == ["nice", "-n", "10"]
    assert cmd[-3:] == ["-f", "mp4", "/out.tmp"]
    assert video.argv("ffmpeg", "/in", "/out", p, nice=None)[0] == "ffmpeg"
    assert video.timeout_for(info(duration=10), "remux") == 70
    assert video.timeout_for(info(duration=10), "transcode") == 320
    assert video.timeout_for(None, "transcode") == 1800


# --- real runs ---------------------------------------------------------------------------------


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, check=False, timeout=120)


def _probe(path):
    return json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                            str(path)]).stdout)


def _preview(tmp_path, src, height=720):
    caps = video.capabilities("ffmpeg")
    i = video.stream_info(video.probe(str(src)))
    p = video.plan(i, caps, height=height)
    out = tmp_path / "preview.mp4"
    proc = _run(video.argv("ffmpeg", str(src), str(out), p))
    assert proc.returncode == 0, proc.stderr.decode()
    return p, out


@needs_ffmpeg
@needs_encoder("mpeg4")
@needs_h264_encoder()
def test_real_transcode_of_mpeg4_with_pcm_audio(tmp_path):
    src = make_video(tmp_path / "src.mov", duration=1, size=(640, 480), audio="pcm_s16le")
    p, out = _preview(tmp_path, src, height=240)
    assert p.mode == "transcode"
    streams = _probe(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next(s for s in streams if s["codec_type"] == "audio")
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert (v["width"], v["height"]) == (320, 240)
    assert a["codec_name"] == "aac"
    data = out.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")       # +faststart


@needs_ffmpeg
@needs_h264_encoder()
@needs_encoder("aac")
def test_real_remux_of_small_h264(tmp_path):
    enc = video.capabilities("ffmpeg").h264_encoder
    src = make_video(tmp_path / "src.mp4", duration=1, size=(320, 240), codec=enc, audio="aac")
    p, out = _preview(tmp_path, src)
    assert p.mode == "remux"
    v = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "h264"


@needs_ffmpeg
@needs_encoder("mpeg4")
@needs_h264_encoder()
@needs_tonemap()
def test_real_tone_mapping_chain_runs(tmp_path):
    # An 8-bit stand-in tagged as HLG: enough to prove the filter chain is
    # accepted and produces a BT.709 file, which is what can break.
    # setparams tags the frames, so the encoder and the container carry it.
    src = make_video(tmp_path / "hlg.mp4", duration=1, size=(320, 240), extra=[
        "-vf", "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc"])
    assert video.stream_info(video.probe(str(src))).hdr
    p, out = _preview(tmp_path, src)
    assert p.tonemapped
    v = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
    assert v["pix_fmt"] == "yuv420p"
    assert v.get("color_transfer") in (None, "bt709")


def test_probe_of_a_missing_file_raises():
    with pytest.raises(video.VideoError):
        video.probe("/nonexistent/file.mov")
