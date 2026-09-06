"""Storyboard (contact-sheet) composition.

Turns a list of extracted frames into labeled grid sheets that let an AI read
the video's timeline without opening every frame file. This module is
deliberately pipeline-independent: it takes tile specs and file paths and
returns image bytes, so it is trivially testable and reusable by both the
pipeline step and the regenerate task.

Design notes that matter:

* **Sheets are an index, not a detail view.** Vision models downscale large
  images, so a 24-tile sheet cannot simultaneously be a place to read small UI
  text. Tiles are sized for timeline comprehension and every tile records the
  path of its full-resolution source frame in the manifest.
* **Aspect ratio is never violated.** Frames are fitted into the tile box and
  letterboxed; nothing is cropped or stretched.
* **Memory is bounded.** `compose_sheet` opens, downscales, pastes and closes
  one frame at a time — a 2000-frame job never has more than one full-size
  frame in memory.
* **One bad frame must not lose the sheet.** A missing, truncated or corrupt
  file renders a labeled placeholder tile instead of raising.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("storyboard")

# Candidate fonts, best first. DejaVu is installed in the worker image via
# fonts-dejavu-core; the others cover dev machines that have something else.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)

# Dark neutral sheet so video content is visually separate from the chrome.
THEMES = {
    "dark": {
        "background": (14, 17, 22),
        "tile_background": (0, 0, 0),
        "caption_background": (26, 31, 40),
        "border": (58, 68, 84),
        "primary_text": (255, 255, 255),
        "secondary_text": (168, 180, 198),
        "placeholder_background": (46, 26, 26),
    },
    "light": {
        "background": (240, 242, 245),
        "tile_background": (255, 255, 255),
        "caption_background": (226, 230, 236),
        "border": (176, 184, 196),
        "primary_text": (16, 20, 26),
        "secondary_text": (78, 88, 102),
        "placeholder_background": (250, 226, 226),
    },
}


@dataclass
class TileSpec:
    """One tile: an image to draw plus the labels that go under it."""

    source_path: Path | None
    frame_number: int
    timestamp_seconds: float
    caption: str | None = None
    subtitle: str | None = None  # e.g. "SEG 07" or "SCENE 3"
    source_frame_rel: str = ""  # path recorded in the manifest
    extra: dict = field(default_factory=dict)


@dataclass
class SheetPlan:
    """Resolved geometry for one sheet of tiles."""

    columns: int
    rows: int
    tile_width: int
    tile_height: int
    caption_height: int
    sheet_width: int
    sheet_height: int
    tiles_per_sheet: int


def format_timestamp(seconds: float) -> str:
    """MM:SS.mmm — the label format used on tiles and in the manifest."""
    if seconds is None or seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"


def resolve_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """First available TrueType font at `size`.

    Falls back to PIL's built-in bitmap font (which ignores size and is tiny)
    with a warning, so a dev box or test runner without fonts still produces
    sheets rather than crashing.
    """
    candidates = _FONT_CANDIDATES if bold else _FONT_CANDIDATES[1:] + _FONT_CANDIDATES[:1]
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    logger.warning(
        "No TrueType font found (looked in %s); storyboard captions will use PIL's "
        "bitmap fallback and be hard to read. Install fonts-dejavu-core.",
        ", ".join(_FONT_CANDIDATES),
    )
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def wrap_caption(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int
) -> list[str]:
    """Word-wrap `text` to at most `max_lines` lines of `max_width` pixels.

    Only the final line is ellipsised, and only when text genuinely remains —
    the complete text is always preserved in the manifest regardless.
    """
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)

    consumed = len(" ".join(lines).split())
    if consumed < len(words) and lines:
        last = lines[-1]
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


def plan_sheets(
    *,
    frame_width: int,
    frame_height: int,
    columns: int | None = None,
    sheet_width: int = 2400,
    max_tiles_per_sheet: int = 24,
    max_sheet_height: int = 4200,
    include_captions: bool = True,
    margin: int = 18,
    gap: int = 12,
    header_height: int = 54,
) -> SheetPlan:
    """Resolve grid geometry from the source frame's shape.

    Column count defaults by orientation: portrait frames get fewer columns
    (tiles are tall, so a wide grid would make an absurdly tall sheet), and the
    row count is additionally bounded by `max_sheet_height` so a sheet never
    degenerates into an unreadable ribbon.
    """
    frame_width = max(int(frame_width or 1), 1)
    frame_height = max(int(frame_height or 1), 1)
    portrait = frame_height > frame_width

    if columns is None:
        columns = 5 if portrait else 6
    columns = max(1, int(columns))

    usable = sheet_width - 2 * margin - gap * (columns - 1)
    tile_width = max(80, usable // columns)
    tile_height = max(60, round(tile_width * frame_height / frame_width))

    caption_height = 0
    if include_captions:
        # Room for: frame number line, timestamp line, then up to 2 caption
        # lines. Sized generously because vision models downscale big sheets —
        # type that looks fine at 2400px can vanish at 1568px.
        caption_height = max(72, round(tile_width * 0.30))
        caption_height = min(caption_height, 190)

    row_height = tile_height + caption_height + gap
    available_height = max_sheet_height - 2 * margin - header_height
    rows_by_height = max(1, available_height // row_height)
    rows_by_count = max(1, max_tiles_per_sheet // columns)
    rows = max(1, min(rows_by_height, rows_by_count))

    tiles_per_sheet = columns * rows
    sheet_height = 2 * margin + header_height + rows * row_height - gap

    return SheetPlan(
        columns=columns,
        rows=rows,
        tile_width=tile_width,
        tile_height=tile_height,
        caption_height=caption_height,
        sheet_width=sheet_width,
        sheet_height=sheet_height,
        tiles_per_sheet=tiles_per_sheet,
    )


def chunk_tiles(tiles: list[TileSpec], per_sheet: int) -> list[list[TileSpec]]:
    """Split tiles into sheet-sized pages, preserving order."""
    per_sheet = max(1, per_sheet)
    return [tiles[i : i + per_sheet] for i in range(0, len(tiles), per_sheet)] or [[]]


def _load_fitted(path: Path | None, box_w: int, box_h: int) -> Image.Image | None:
    """Open one frame, downscale to fit the tile box, return it. None on any
    failure — callers render a placeholder instead. Uses draft() so a large
    JPEG is decoded at reduced scale rather than in full."""
    if path is None or not Path(path).is_file():
        return None
    try:
        with Image.open(path) as img:
            img.draft("RGB", (box_w, box_h))  # cheap DCT-scaled JPEG decode
            img = img.convert("RGB")
            img.thumbnail((box_w, box_h), Image.LANCZOS)
            return img.copy()
    except Exception as exc:  # noqa: BLE001 - one bad frame must not lose the sheet
        logger.warning("Could not read frame %s for storyboard: %s", path, exc)
        return None


def _draw_tile(
    sheet: Image.Image,
    draw: ImageDraw.ImageDraw,
    tile: TileSpec,
    *,
    x: int,
    y: int,
    plan: SheetPlan,
    theme: dict,
    fonts: dict,
) -> bool:
    """Draw one tile at (x, y). Returns False when a placeholder was used."""
    tw, th = plan.tile_width, plan.tile_height
    ok = True

    image = _load_fitted(tile.source_path, tw, th)
    if image is None:
        ok = False
        draw.rectangle([x, y, x + tw, y + th], fill=theme["placeholder_background"])
        msg = "FRAME UNAVAILABLE"
        w = _text_width(draw, msg, fonts["small"])
        draw.text(
            (x + (tw - w) // 2, y + th // 2 - 8), msg, font=fonts["small"], fill=theme["primary_text"]
        )
    else:
        draw.rectangle([x, y, x + tw, y + th], fill=theme["tile_background"])
        # Letterbox: centre the fitted image inside the tile box.
        off_x = x + (tw - image.width) // 2
        off_y = y + (th - image.height) // 2
        sheet.paste(image, (off_x, off_y))
        image.close()

    if plan.caption_height:
        cap_y = y + th
        draw.rectangle(
            [x, cap_y, x + tw, cap_y + plan.caption_height], fill=theme["caption_background"]
        )
        pad = 6
        line_y = cap_y + 4
        label = f"FRAME {tile.frame_number:03d}"
        if tile.subtitle:
            label = f"{label}  {tile.subtitle}"
        draw.text((x + pad, line_y), label, font=fonts["label"], fill=theme["primary_text"])
        line_y += fonts["label_height"]
        draw.text(
            (x + pad, line_y),
            format_timestamp(tile.timestamp_seconds),
            font=fonts["time"],
            fill=theme["primary_text"],
        )
        line_y += fonts["time_height"]
        if tile.caption:
            remaining = plan.caption_height - (line_y - cap_y) - 4
            max_lines = max(0, remaining // fonts["caption_height"])
            for text_line in wrap_caption(
                draw, tile.caption, fonts["caption"], tw - 2 * pad, max_lines
            ):
                draw.text(
                    (x + pad, line_y), text_line, font=fonts["caption"], fill=theme["secondary_text"]
                )
                line_y += fonts["caption_height"]

    # Thin border around the whole tile (image + caption).
    draw.rectangle(
        [x, y, x + tw, y + th + plan.caption_height], outline=theme["border"], width=1
    )
    return ok


def _build_fonts(plan: SheetPlan) -> dict:
    # Base sizes are deliberately large relative to the tile: a 2400px sheet is
    # typically resized to ~1568px by a vision model, so anything under ~20px
    # here becomes unreadable there.
    scale = max(0.6, min(1.6, plan.tile_width / 460))
    label_size = max(15, round(26 * scale))
    time_size = max(15, round(25 * scale))
    caption_size = max(13, round(22 * scale))
    return {
        "label": resolve_font(label_size, bold=True),
        "label_height": label_size + 4,
        "time": resolve_font(time_size, bold=True),
        "time_height": time_size + 4,
        "caption": resolve_font(caption_size),
        "caption_height": caption_size + 3,
        "small": resolve_font(max(11, round(14 * scale))),
        "header": resolve_font(max(14, round(22 * scale)), bold=True),
    }


def compose_sheet(
    tiles: list[TileSpec],
    plan: SheetPlan,
    *,
    title: str = "",
    theme_name: str = "dark",
    quality: int = 90,
) -> tuple[bytes, int]:
    """Render one sheet to JPEG bytes.

    Returns (jpeg_bytes, placeholder_count). Tiles are laid out strictly left
    to right, then top to bottom, so reading order is chronological order.
    """
    import io

    theme = THEMES.get(theme_name) or THEMES["dark"]
    fonts = _build_fonts(plan)

    # Trim the sheet to the rows actually used, so a half-full last page isn't
    # mostly empty background.
    used_rows = max(1, -(-len(tiles) // plan.columns)) if tiles else 1
    row_height = plan.tile_height + plan.caption_height + 12
    margin, header = 18, 54
    height = 2 * margin + header + used_rows * row_height - 12

    sheet = Image.new("RGB", (plan.sheet_width, height), theme["background"])
    draw = ImageDraw.Draw(sheet)

    if title:
        draw.text((margin, margin), title, font=fonts["header"], fill=theme["primary_text"])

    placeholders = 0
    for i, tile in enumerate(tiles):
        col = i % plan.columns
        row = i // plan.columns
        x = margin + col * (plan.tile_width + 12)
        y = margin + header + row * row_height
        if not _draw_tile(sheet, draw, tile, x=x, y=y, plan=plan, theme=theme, fonts=fonts):
            placeholders += 1

    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
    sheet.close()
    return buffer.getvalue(), placeholders
