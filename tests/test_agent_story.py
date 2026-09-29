"""Storyboards: where the frames come from, the one ffmpeg that takes them,
and real strips from generated clips where this machine's ffmpeg can make
them (Fedora's has no HEVC decoder: those paths are tested with a fake
probe)."""

from __future__ import annotations

import subprocess

import pytest
from agent_helpers import (
    add_photo,
    make_video,
    needs_db,
    needs_encoder,
    needs_exiftool,
    needs_ffmpeg,
    needs_tonemap,
    use_db,
    use_library,
)
from PIL import Image
from sqlalchemy import select

from agent import scan, story, video
from agent.pipeline import Pipeline, pending
from core.config import get_settings
from core.media import STORY_FRAMES, STORY_HEIGHT, story_path
from core.models import Photo


def test_frame_width():
    assert story.frame_width(1920, 1080) == 320
    assert story.frame_width(1080, 1920) == 102            # 101.25, made even
    assert story.frame_width(1440, 1920) == 136
    assert story.frame_width(400, 711, 180) % 2 == 0       # zscale wants even sides
    assert story.frame_width(32000, 1000) == story.MAX_FRAME_WIDTH


def test_frame_rate_and_count():
    assert story.frame_rate({"avg_frame_rate": "30/1"}) == 30
    assert story.frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "6775/226"}) == pytest.approx(29.98, abs=0.01)
    assert story.frame_rate({"avg_frame_rate": "90000/1"}) is None
    assert story.frame_count({"nb_frames": "254"}, 8.5, 30) == 254
    assert story.frame_count({}, 2.0, 10) == 20
    assert story.frame_count({}, None, None) is None


K4 = 3840 * 2160


def test_long_or_large_clips_are_taken_from_keyframes():
    p = story.plan(600.0, 18000, K4)
    assert p.keyframes
    assert p.times == tuple(60.0 * (i + 0.5) for i in range(STORY_FRAMES))
    # Ten minutes of 320x240 is still too much to decode for ten frames.
    assert story.plan(600.0, 18000, 320 * 240).keyframes
    # A short 4K clip: more than 30 frames of 4K is past the budget, and a
    # 2.5 s clip cannot have more than five keyframes worth seeking to.
    p = story.plan(2.5, 76, K4)
    assert p.keyframes and len(p.times) == 5


def test_short_small_clips_are_decoded_once():
    p = story.plan(1.0, 10, 320 * 240)
    assert not p.keyframes
    assert p.times == pytest.approx(tuple(0.1 * (i + 0.5) for i in range(10)))
    assert len(story.plan(0.3, 3, 320 * 240).times) == 3           # three frames: three
    assert not story.plan(3.0, 90, 1920 * 1080).keyframes           # 3 s of 1080p
    assert story.plan(8.0, 240, 1920 * 1080).keyframes              # 8 s: past the budget
    assert story.plan(None, None, K4) == story.Plan((0.0,), False)  # nothing known: the start


def _task(tmp_path, **kw):
    return story.StoryTask(photo_id=1, src=str(tmp_path / "in.mov"), dest=str(tmp_path / "out.webp"), **kw)


def test_keyframe_argv_is_one_ffmpeg_with_an_input_per_frame(tmp_path):
    p = story.Plan((6.0, 18.0, 30.0), True)
    cmd = story.argv(_task(tmp_path), p, 320, hdr=False, stream=1)
    assert cmd.count("-i") == 3 and cmd.count("-threads") == 3
    assert cmd.count("-skip_frame") == 3 and cmd.count("-noaccurate_seek") == 3
    # -ss before its -i: a seek in the file, not a decode up to the time.
    assert cmd.index("-ss") < cmd.index("-i")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("[1:1]trim=end_frame=1") == 1 and "[2:1]" in graph
    assert "scale=320:180,setsar=1,format=rgb24" in graph and graph.endswith("hstack=inputs=3")
    assert cmd[-5:] == ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    hdr = story.argv(_task(tmp_path), p, 320, hdr=True)
    assert video.TONEMAP in hdr[hdr.index("-filter_complex") + 1]
    no_zscale = story.argv(_task(tmp_path, tonemap=False), p, 320, hdr=True)
    assert video.TONEMAP not in no_zscale[no_zscale.index("-filter_complex") + 1]


def test_decode_argv_is_one_pass_picking_frames(tmp_path):
    p = story.plan(1.0, 10, 320 * 240)
    cmd = story.argv(_task(tmp_path), p, 240, hdr=False, stream=0, fps=10.0)
    assert cmd.count("-i") == 1 and "-ss" not in cmd and "-skip_frame" not in cmd
    vf = cmd[cmd.index("-vf") + 1]
    # Half a frame early: frame k of a ten-frame clip is picked for time k.
    assert vf.startswith("select='gte(t-start_t\\,0.000000+selected_n*0.100000)'")
    assert cmd[cmd.index("-frames:v") + 1] == "10"


def test_repeated_keyframes_are_dropped():
    red, blue = Image.new("RGB", (4, 4), (255, 0, 0)), Image.new("RGB", (4, 4), (0, 0, 255))
    assert len(story.distinct([red, red.copy(), blue, blue, red])) == 3
    raw = red.tobytes() + blue.tobytes()
    frames = story.frames_of(raw, 4, 4, side_by_side=False)
    assert [f.getpixel((0, 0)) for f in frames] == [(255, 0, 0), (0, 0, 255)]
    strip = Image.new("RGB", (8, 4))
    strip.paste(red, (0, 0))
    strip.paste(blue, (4, 0))
    frames = story.frames_of(strip.tobytes(), 4, 4, side_by_side=True)
    assert [f.getpixel((0, 0)) for f in frames] == [(255, 0, 0), (0, 0, 255)]
    assert story.frames_of(b"", 4, 4, side_by_side=True) == []


def test_hevc_without_a_decoder_says_what_to_do(tmp_path, monkeypatch):
    info = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080}]}
    monkeypatch.setattr(video, "probe", lambda path, ffprobe="ffprobe": info)
    with pytest.raises(story.StoryError, match="cannot decode HEVC; run the agent in its container"):
        story.render(_task(tmp_path, hevc=False))


# --- real strips ---------------------------------------------------------------------------


def _frames(path, n):
    with Image.open(path) as img:
        strip = img.convert("RGB")
    w = strip.width // n
    return strip, [strip.crop((i * w, 0, (i + 1) * w, strip.height)).tobytes() for i in range(n)]


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_a_short_clip_gets_ten_different_frames(tmp_path):
    src = make_video(tmp_path / "in.mov", duration=1, size=(320, 240), rate=10, extra=["-g", "5"])
    assert story.render(_task(tmp_path)) == STORY_FRAMES
    strip, frames = _frames(tmp_path / "out.webp", STORY_FRAMES)
    assert strip.size == (240 * STORY_FRAMES, STORY_HEIGHT)
    assert len(set(frames)) == STORY_FRAMES                 # testsrc changes every frame
    assert src.exists() and not list(tmp_path.glob(".*.tmp"))


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_a_clip_with_three_frames_gets_three(tmp_path):
    make_video(tmp_path / "in.mov", duration=0.3, size=(160, 120), rate=10)
    assert story.render(_task(tmp_path)) == 3
    strip, frames = _frames(tmp_path / "out.webp", 3)
    assert strip.size == (240 * 3, STORY_HEIGHT) and len(set(frames)) == 3


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_a_long_clip_is_seeked_to_keyframes(tmp_path, monkeypatch):
    make_video(tmp_path / "in.mov", duration=60, size=(160, 120), rate=5, extra=["-g", "5"])
    monkeypatch.setattr(story, "DECODE_BUDGET", 0)          # as if it were 4K
    seen = []
    real = story.argv
    monkeypatch.setattr(story, "argv", lambda *a, **kw: seen.append(a[1]) or real(*a, **kw))
    assert story.render(_task(tmp_path)) == STORY_FRAMES
    assert seen[-1].keyframes and len(seen) == 1
    _, frames = _frames(tmp_path / "out.webp", STORY_FRAMES)
    assert len(set(frames)) == STORY_FRAMES


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_seeks_that_land_on_one_keyframe_count_once(tmp_path, monkeypatch):
    # Keyframes every 2 s in a 6 s clip: ten seeks, three different frames.
    make_video(tmp_path / "in.mov", duration=6, size=(160, 120), rate=5, extra=["-g", "10"])
    monkeypatch.setattr(story, "DECODE_BUDGET", 0)
    assert story.render(_task(tmp_path)) == 3
    with Image.open(tmp_path / "out.webp") as img:
        assert img.size == (3 * 240, STORY_HEIGHT)


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_rotation_is_applied(tmp_path):
    flat = make_video(tmp_path / "flat.mov", duration=1, size=(320, 240))
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-display_rotation:v:0", "90",
                    "-i", str(flat), "-c", "copy", str(tmp_path / "in.mov")], check=True, capture_output=True)
    n = story.render(_task(tmp_path))
    with Image.open(tmp_path / "out.webp") as img:
        assert img.size == (136 * n, STORY_HEIGHT)          # portrait frames


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_a_pass_that_finds_no_frame_falls_back_to_keyframes(tmp_path, monkeypatch):
    # A container whose duration promises more than it holds: a pass that
    # picks from past the last frame yields nothing (ffmpeg exits 0 with no
    # output), and the keyframe run still makes the strip.
    make_video(tmp_path / "in.mov", duration=1, size=(160, 120), rate=10)
    monkeypatch.setattr(story, "plan", lambda *a, **kw: story.Plan((30.0, 40.0), False))
    seen = []
    real = story.argv
    monkeypatch.setattr(story, "argv", lambda *a, **kw: seen.append(a[1].keyframes) or real(*a, **kw))
    assert story.render(_task(tmp_path)) == 1          # both seeks find the last keyframe
    assert seen == [False, True]
    with Image.open(tmp_path / "out.webp") as img:
        assert img.size == (240, STORY_HEIGHT)


@needs_ffmpeg
@needs_encoder("mpeg4")
@needs_tonemap()
def test_hdr_is_tone_mapped(tmp_path):
    make_video(tmp_path / "in.mov", duration=1, size=(320, 240), extra=[
        "-vf", "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc"])
    assert video.stream_info(video.probe(str(tmp_path / "in.mov"))).hdr
    assert story.render(_task(tmp_path)) == STORY_FRAMES


@needs_ffmpeg
def test_not_a_video(tmp_path):
    (tmp_path / "in.mov").write_bytes(b"nothing to see")
    with pytest.raises(video.VideoError):
        story.render(_task(tmp_path))


# --- the stage -------------------------------------------------------------------------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@needs_db
@needs_exiftool
@needs_encoder("mpeg4")
def test_the_stage_draws_videos_but_not_companions(db, lib):
    make_video(lib / "clip.mov", duration=2, size=(320, 240))
    make_video(lib / "IMG_1.MOV", duration=1, size=(320, 240))
    scan.scan(db, get_settings())
    pipe = Pipeline(get_settings(), once=True)
    try:
        pipe.meta_batch(db)
        motion = db.scalar(select(Photo).where(Photo.name == "IMG_1.MOV"))
        motion.is_companion = True
        db.commit()
        pipe.drain(db)
    finally:
        pipe.close()
    db.expire_all()
    rows = {p.name: p for p in db.scalars(select(Photo))}
    clip = rows["clip.mov"]
    assert clip.story_sig == clip.sig and clip.story_frames == STORY_FRAMES
    assert story_path(clip.sig).is_file()
    assert rows["IMG_1.MOV"].story_sig == ""
    assert pending(db, get_settings())["story"] == 0


@needs_db
def test_the_stage_parks_hevc_on_a_host_without_a_decoder(db, lib):
    pipe = Pipeline(get_settings(), once=True)
    try:
        pipe.caps = video.Caps(found=True, decoders=frozenset({"h264"}))
        row = add_photo(db, "IMG_9.MOV", video_codec="hevc", duration=2.0)
        row.meta_sig = row.thumb_sig = row.sig
        db.commit()
        (lib / "IMG_9.MOV").write_bytes(b"x")
        assert pipe.story_batch(db) == 1
    finally:
        pipe.close()
    db.expire_all()
    row = db.get(Photo, row.id)
    assert (row.error_stage, row.fail_count) == ("story", 1)
    assert row.error == video.HEVC_MISSING
