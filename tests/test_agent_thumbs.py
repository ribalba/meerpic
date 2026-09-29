"""Thumbnails: sizes, orientation, transparency, HEIC, and video frames.

Called directly rather than through the process pool: the pool is plumbing
(tested with the pipeline), the drawing is what these check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_helpers import heic, jpeg, make_video, needs_encoder, needs_ffmpeg, png
from PIL import Image, UnidentifiedImageError

from agent import thumbs, video
from core.media import THUMB_EDGE, THUMB_MAX_LONG


@pytest.mark.parametrize("size, expected", [
    ((4032, 3024), (533, THUMB_EDGE)),
    ((3024, 4032), (THUMB_EDGE, 533)),
    ((400, 400), (400, 400)),
    ((120, 80), (120, 80)),                           # never upscaled
    ((16000, 2000), (THUMB_MAX_LONG, 200)),           # panorama: long edge capped
    ((1170, 2532), (THUMB_EDGE, 866)),
])
def test_target_size(size, expected):
    assert thumbs.target_size(*size) == expected


def _task(src: Path, dest: Path, kind: str = "photo", **kw) -> thumbs.ThumbTask:
    return thumbs.ThumbTask(photo_id=1, src=str(src), dest=str(dest), kind=kind, **kw)


def test_jpeg_with_orientation_is_turned_and_reports_the_displayed_size(tmp_path):
    # Left half red, right half blue, stored landscape with "rotate 90 CW".
    img = Image.new("RGB", (800, 600), (255, 0, 0))
    img.paste((0, 0, 255), (400, 0, 800, 600))
    src = jpeg(tmp_path / "turned.jpg", image=img, orientation=6)
    dest = tmp_path / "out" / "ab" / "abcdef.webp"
    assert thumbs.render(_task(src, dest)) == (600, 800, None)
    with Image.open(dest) as out:
        assert out.format == "WEBP"
        assert out.size == (400, 533)
        rgb = out.convert("RGB")
        # After a 90 degree clockwise turn the left (red) half is on top.
        top, bottom = rgb.getpixel((200, 50)), rgb.getpixel((200, 480))
    assert top[0] > 200 and top[2] < 60
    assert bottom[2] > 200 and bottom[0] < 60
    assert not any(p.name.endswith(".tmp") for p in dest.parent.iterdir())


def test_large_jpeg_uses_draft_but_keeps_full_size(tmp_path):
    src = jpeg(tmp_path / "big.jpg", size=(4000, 3000))
    dest = tmp_path / "big.webp"
    assert thumbs.render(_task(src, dest)) == (4000, 3000, None)
    with Image.open(dest) as out:
        assert out.size == (533, 400)


def test_transparent_png_goes_onto_white(tmp_path):
    src = png(tmp_path / "logo.png", size=(80, 60), color=(0, 0, 0, 0))
    dest = tmp_path / "logo.webp"
    assert thumbs.render(_task(src, dest)) == (80, 60, None)
    with Image.open(dest) as out:
        assert out.convert("RGB").getpixel((40, 30)) == pytest.approx((255, 255, 255), abs=3)


def test_palette_and_grey_and_16_bit_images(tmp_path):
    Image.new("P", (50, 40), 3).save(tmp_path / "p.gif")
    Image.new("L", (50, 40), 128).save(tmp_path / "l.png")
    Image.new("I;16", (50, 40), 40000).save(tmp_path / "i16.png")
    Image.new("CMYK", (50, 40), (0, 255, 255, 0)).save(tmp_path / "cmyk.jpg")
    for name in ("p.gif", "l.png", "i16.png", "cmyk.jpg"):
        dest = tmp_path / f"{name}.webp"
        assert thumbs.render(_task(tmp_path / name, dest)) == (50, 40, None)
        with Image.open(dest) as out:
            assert out.mode == "RGB"
    with Image.open(tmp_path / "i16.png.webp") as out:
        assert 140 <= out.getpixel((10, 10))[0] <= 175       # 40000/65535, not clipped white


def test_heic(tmp_path):
    src = heic(tmp_path / "IMG_0001.HEIC", size=(960, 640))
    dest = tmp_path / "heic.webp"
    assert thumbs.render(_task(src, dest)) == (960, 640, None)
    with Image.open(dest) as out:
        assert out.size == (600, 400)


def test_not_an_image_raises(tmp_path):
    src = tmp_path / "broken.jpg"
    src.write_bytes(b"\x00" * 100)
    with pytest.raises(UnidentifiedImageError):
        thumbs.render(_task(src, tmp_path / "x.webp"))
    assert not (tmp_path / "x.webp").exists()


# --- videos --------------------------------------------------------------------------


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_video_frame(tmp_path):
    src = make_video(tmp_path / "clip.mp4", duration=2, size=(640, 480))
    dest = tmp_path / "clip.webp"
    assert thumbs.render(_task(src, dest, kind="video", duration=2.0))[:2] == (640, 480)
    with Image.open(dest) as out:
        assert out.size == (533, 400)


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_rotated_video_reports_portrait(tmp_path):
    flat = make_video(tmp_path / "flat.mp4", duration=1, size=(320, 240))
    src = tmp_path / "turned.mp4"
    import subprocess

    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-display_rotation:v:0", "90",
                    "-i", str(flat), "-c", "copy", str(src)], check=True, capture_output=True)
    dest = tmp_path / "turned.webp"
    w, h, duration = thumbs.render(_task(src, dest, kind="video"))
    assert (w, h) == (240, 320)
    assert duration == pytest.approx(1.0, abs=0.15)
    with Image.open(dest) as out:
        assert out.size == (240, 320)


@needs_ffmpeg
@needs_encoder("mpeg4")
def test_very_short_video_still_gets_a_frame(tmp_path):
    src = make_video(tmp_path / "blink.mp4", duration=0.2, rate=10)
    dest = tmp_path / "blink.webp"
    # A wrong duration from the metadata: the seek lands past the end.
    assert thumbs.render(_task(src, dest, kind="video", duration=30.0))[:2] == (320, 240)
    assert dest.is_file()


def test_hevc_without_a_decoder_says_what_to_do(tmp_path, monkeypatch):
    info = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080}]}
    monkeypatch.setattr(video, "probe", lambda path, ffprobe="ffprobe": info)
    with pytest.raises(thumbs.ThumbError, match="cannot decode HEVC; run the agent in its container"):
        thumbs.render(_task(tmp_path / "x.mov", tmp_path / "x.webp", kind="video", hevc=False))


def test_hdr_frame_is_scaled_to_even_sides_before_the_tone_map(tmp_path, monkeypatch):
    """zscale refuses an odd-sided 4:2:0 frame, and a portrait 4K iPhone video
    thumbnails to 400x711. The frame is drawn at 400x712 and resized after."""
    seen = []

    class Done:
        returncode = 0
        stdout = b"png"
        stderr = b""

    def fake_run(cmd, **_kw):
        seen.append(cmd)
        return Done()

    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    task = _task(tmp_path / "in.mov", tmp_path / "out.webp", kind="video")
    thumbs._frame(task, 1.0, (400, 711), hdr=True)
    chain = seen[-1][seen[-1].index("-vf") + 1]
    assert chain.startswith("scale=400:712,")
    assert video.TONEMAP in chain

    thumbs._frame(task, 1.0, (400, 711), hdr=False)
    assert seen[-1][seen[-1].index("-vf") + 1].startswith("scale=400:711,")
