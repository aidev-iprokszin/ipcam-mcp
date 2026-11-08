import io
import random

import pytest
from PIL import Image, ImageDraw

from ipcam_mcp.imaging import (
    LIGHTING_PCT,
    NO_CHANGE_PCT,
    REGIONS,
    compare,
    crop_box,
    crop_jpeg,
)


def noisy(w=640, h=360, seed=1, offset=0):
    rnd = random.Random(seed)
    img = Image.new("L", (w, h))
    img.putdata([128 + offset + rnd.randint(-5, 5) for _ in range(w * h)])
    return img.convert("RGB")


def jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_region_boxes():
    assert crop_box(None, "top-right") == pytest.approx((2 / 3, 0, 1 / 3, 1 / 3))
    assert crop_box(None, "center") == pytest.approx((1 / 3, 1 / 3, 1 / 3, 1 / 3))
    assert crop_box(None, None) is None
    assert len(REGIONS) == 9


@pytest.mark.parametrize(
    "crop,region",
    [
        ([0, 0, 0.5, 0.5], "top"),
        (None, "middle"),
        ([0.5, 0.5, 0.6, 0.1], None),
        ([0, 0, 0, 0.5], None),
        ([0, 0, 0.5], None),
    ],
)
def test_crop_box_errors(crop, region):
    with pytest.raises(ValueError):
        crop_box(crop, region)


def test_crop_box_tolerance():
    box = crop_box([0.5, 0, 0.5000001, 1], None)
    assert box == pytest.approx((0.5, 0, 0.5, 1))
    assert box[0] + box[2] <= 1.0


def test_crop_jpeg_native_size(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (1920, 1080), "gray").save(p, "JPEG")
    data, size = crop_jpeg(p, (0.5, 0.5, 0.25, 0.25))
    assert size == (480, 270)
    assert Image.open(io.BytesIO(data)).size == (480, 270)
    _, size = crop_jpeg(p, None)
    assert size == (1280, 720)


def test_crop_jpeg_region_rounding(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (64, 48), "gray").save(p, "JPEG")
    _, size = crop_jpeg(p, crop_box(None, "top-right"))
    assert size == (21, 16)


def test_compare_identical():
    r = compare(jpeg(noisy()), jpeg(noisy(seed=2)))
    assert r.changed_pct < NO_CHANGE_PCT
    assert r.cells == []
    assert r.box is None
    assert not r.aspect_mismatch


def test_compare_brightness_shift():
    r = compare(jpeg(noisy()), jpeg(noisy(offset=40)))
    assert r.changed_pct < NO_CHANGE_PCT


def test_compare_square_top_right():
    cur = noisy()
    ImageDraw.Draw(cur).rectangle([500, 10, 599, 109], fill="white")
    r = compare(jpeg(noisy()), jpeg(cur))
    assert r.cells == ["top-right"]
    assert 0.75 <= r.box[0] <= 0.8
    img = Image.open(io.BytesIO(r.annotated))
    assert img.format == "JPEG"
    assert img.width <= 1280


def test_compare_big_change():
    cur = Image.new("RGB", (640, 360), "black")
    ImageDraw.Draw(cur).rectangle([320, 0, 639, 359], fill="white")
    r = compare(jpeg(noisy()), jpeg(cur))
    assert r.changed_pct > LIGHTING_PCT


def test_compare_aspect_mismatch():
    r = compare(jpeg(noisy(640, 480)), jpeg(noisy(640, 360)))
    assert r.aspect_mismatch is True


def test_compare_sees_small_detail_at_native_size():
    # A 4x4 px light in a 1280x720 frame: lost if the frame is scaled down first.
    cur = noisy(1280, 720)
    ImageDraw.Draw(cur).rectangle([1000, 100, 1003, 103], fill="white")
    r = compare(jpeg(noisy(1280, 720)), jpeg(cur))
    assert r.box is not None
    x, y, w, h = r.box
    assert x <= 1000 / 1280 < x + w
    assert y <= 100 / 720 < y + h
    assert w < 0.02 and h < 0.03


def test_compare_reference_of_other_size():
    # e.g. a 720p baseline compared with a frame after the camera moved to 2K
    big = noisy(1280, 720).resize((1920, 1080))
    r = compare(jpeg(noisy(1280, 720)), jpeg(big))
    assert r.changed_pct < NO_CHANGE_PCT
    assert r.box is None
    assert not r.aspect_mismatch
