"""Crop and frame-compare helpers (Pillow only)."""

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

REGIONS: tuple[str, ...] = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)

Box = tuple[float, float, float, float]

BLUR_RADIUS = 2
PIXEL_THRESHOLD = 30
DILATE_SIZE = 5
CELL_THRESHOLD_PCT = 2.0
NO_CHANGE_PCT = 0.5
LIGHTING_PCT = 50.0
PREVIEW_WIDTH = 1280
JPEG_QUALITY = 85
_TOL = 1e-6


def crop_box(crop: Sequence[float] | None, region: str | None) -> Box | None:
    """Resolve `crop` ([x, y, w, h] fractions) or a named `region` to a box."""
    if crop is not None and region is not None:
        raise ValueError("Give either crop or region, not both.")
    if region is not None:
        if region not in REGIONS:
            raise ValueError(f"Unknown region {region!r}. Valid: {', '.join(REGIONS)}.")
        i = REGIONS.index(region)
        row, col = divmod(i, 3)
        return (col / 3, row / 3, 1 / 3, 1 / 3)
    if crop is None:
        return None
    if len(crop) != 4:
        raise ValueError("crop must be [x, y, w, h] (4 numbers, fractions 0-1).")
    x, y, w, h = (float(v) for v in crop)
    if x < -_TOL or y < -_TOL or w <= 0 or h <= 0:
        raise ValueError("crop needs x, y >= 0 and w, h > 0.")
    if x + w > 1 + _TOL or y + h > 1 + _TOL:
        raise ValueError("crop must stay inside the frame (x+w <= 1, y+h <= 1).")
    x, y = max(x, 0.0), max(y, 0.0)
    w, h = min(w, 1 - x), min(h, 1 - y)
    return (x, y, w, h)


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def crop_jpeg(
    path: Path, box: Box | None, max_width: int = PREVIEW_WIDTH
) -> tuple[bytes, tuple[int, int]]:
    """Crop `path` to `box` (None = whole), downscale to `max_width`, return JPEG."""
    with Image.open(path) as src:
        img = ImageOps.exif_transpose(src) or src
        img = img.convert("RGB")
    if box is not None:
        w, h = img.size
        x, y, bw, bh = box
        left, top = round(x * w), round(y * h)
        right, bottom = round((x + bw) * w), round((y + bh) * h)
        right, bottom = max(right, left + 1), max(bottom, top + 1)
        img = img.crop((left, top, min(right, w), min(bottom, h)))
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))))
    return _to_jpeg(img), img.size


@dataclass
class CompareResult:
    changed_pct: float
    cells: list[str]
    box: Box | None
    aspect_mismatch: bool
    annotated: bytes


def compare(ref: bytes, cur: bytes) -> CompareResult:
    """Compare two JPEG frames at the current frame's native size.

    The reference is scaled to the current frame if their sizes differ. Blur, threshold
    and dilation work in native pixels, so the detail is whatever the camera sends.
    """
    ref_img = Image.open(io.BytesIO(ref))
    cur_img = Image.open(io.BytesIO(cur))
    cur_rgb = cur_img.convert("RGB")
    mismatch = abs((ref_img.width / ref_img.height) / (cur_img.width / cur_img.height) - 1) > 0.01

    w, h = cur_img.size

    def prep(img: Image.Image) -> Image.Image:
        gray = img.convert("L")
        if gray.size != (w, h):
            gray = gray.resize((w, h))
        return gray.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))

    # signed difference stored with offset 128, then remove the global shift
    diff = ImageChops.subtract(prep(cur_img), prep(ref_img), scale=1, offset=128)
    mean = ImageStat.Stat(diff).mean[0]
    mask = diff.point(lambda v: 255 if abs(v - mean) > PIXEL_THRESHOLD else 0)
    mask = mask.filter(ImageFilter.MaxFilter(DILATE_SIZE))

    total = w * h
    changed = mask.histogram()[255]
    pct = round(changed / total * 100, 1)

    cells: list[str] = []
    rects: list[tuple[int, int, int, int]] = []
    for i, name in enumerate(REGIONS):
        row, col = divmod(i, 3)
        x0, x1 = col * w // 3, (col + 1) * w // 3
        y0, y1 = row * h // 3, (row + 1) * h // 3
        n = mask.crop((x0, y0, x1, y1)).histogram()[255]
        if n / ((x1 - x0) * (y1 - y0)) * 100 > CELL_THRESHOLD_PCT:
            cells.append(name)
            rects.append((x0, y0, x1, y1))

    bbox = mask.getbbox()
    box: Box | None = None
    if bbox:
        box = (bbox[0] / w, bbox[1] / h, (bbox[2] - bbox[0]) / w, (bbox[3] - bbox[1]) / h)

    preview = cur_rgb
    if preview.width > PREVIEW_WIDTH:
        preview = preview.resize(
            (PREVIEW_WIDTH, max(1, round(preview.height * PREVIEW_WIDTH / preview.width)))
        )
    draw = ImageDraw.Draw(preview)
    sx, sy = preview.width / w, preview.height / h
    for x0, y0, x1, y1 in rects:
        draw.rectangle((x0 * sx, y0 * sy, x1 * sx - 1, y1 * sy - 1), outline=(255, 0, 0), width=3)
    return CompareResult(pct, cells, box, mismatch, _to_jpeg(preview))
