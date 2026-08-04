"""Storyboard composition: geometry, wrapping, and failure tolerance.

These tests exercise app/utils/storyboard.py directly — no pipeline, no job —
because the layout maths is where readability is won or lost.
"""
import io

import pytest
from PIL import Image, ImageDraw

from app.utils.storyboard import (
    THEMES,
    SheetPlan,
    TileSpec,
    chunk_tiles,
    compose_sheet,
    format_timestamp,
    plan_sheets,
    resolve_font,
    wrap_caption,
)


def _frame(tmp_path, name: str, size=(720, 1280), color=(30, 90, 160)) -> str:
    path = tmp_path / name
    Image.new("RGB", size, color).save(path, "JPEG", quality=85)
    return str(path)


# ------------------------------------------------------------------ timestamps


def test_format_timestamp():
    assert format_timestamp(0) == "00:00.000"
    assert format_timestamp(12.5) == "00:12.500"
    assert format_timestamp(75.25) == "01:15.250"
    assert format_timestamp(632.125) == "10:32.125"
    # Defensive: never render a negative or None as garbage.
    assert format_timestamp(-1) == "00:00.000"
    assert format_timestamp(None) == "00:00.000"


# ---------------------------------------------------------------------- layout


def test_portrait_gets_fewer_columns_than_landscape():
    """Portrait tiles are tall; a wide grid would make an unreadable ribbon."""
    portrait = plan_sheets(frame_width=720, frame_height=1280)
    landscape = plan_sheets(frame_width=1280, frame_height=720)
    assert portrait.columns < landscape.columns


def test_tile_box_preserves_source_aspect_ratio():
    plan = plan_sheets(frame_width=1080, frame_height=1920)
    source_ratio = 1920 / 1080
    tile_ratio = plan.tile_height / plan.tile_width
    assert abs(tile_ratio - source_ratio) < 0.02


def test_sheet_height_is_bounded_for_portrait():
    """The whole point of the height cap: a 9:16 grid must not run away."""
    plan = plan_sheets(frame_width=720, frame_height=1280, max_sheet_height=4200)
    assert plan.sheet_height <= 4200
    assert plan.rows >= 1


def test_tiles_per_sheet_respects_both_caps():
    plan = plan_sheets(frame_width=1280, frame_height=720, max_tiles_per_sheet=12)
    assert plan.tiles_per_sheet <= 12
    assert plan.tiles_per_sheet == plan.columns * plan.rows


def test_explicit_columns_are_honoured():
    plan = plan_sheets(frame_width=720, frame_height=1280, columns=4)
    assert plan.columns == 4


def test_tiles_stay_large_enough_to_read():
    """A tile that's a postage stamp defeats the purpose."""
    for w, h in ((720, 1280), (1280, 720), (1080, 1080)):
        plan = plan_sheets(frame_width=w, frame_height=h, sheet_width=2400)
        assert plan.tile_width >= 350, f"{w}x{h} produced {plan.tile_width}px tiles"


def test_captions_can_be_disabled():
    with_caps = plan_sheets(frame_width=720, frame_height=1280, include_captions=True)
    without = plan_sheets(frame_width=720, frame_height=1280, include_captions=False)
    assert with_caps.caption_height > 0
    assert without.caption_height == 0


# --------------------------------------------------------------------- chunking


def test_chunk_tiles_preserves_order_and_covers_everything():
    tiles = [TileSpec(None, i, float(i)) for i in range(50)]
    pages = chunk_tiles(tiles, 20)
    assert [len(p) for p in pages] == [20, 20, 10]
    flattened = [t.frame_number for page in pages for t in page]
    assert flattened == list(range(50))  # no duplicates, no gaps, order kept


def test_chunk_tiles_handles_empty_and_single():
    assert chunk_tiles([], 20) == [[]]
    assert len(chunk_tiles([TileSpec(None, 0, 0.0)], 20)) == 1


# ---------------------------------------------------------------------- wrapping


def test_wrap_caption_wraps_and_ellipsises():
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    font = resolve_font(15)

    lines = wrap_caption(draw, "Add a goal counter at the top of the screen", font, 200, 2)
    assert 1 <= len(lines) <= 2
    # Something was dropped, so the last line signals truncation.
    joined = " ".join(lines)
    if "screen" not in joined:
        assert lines[-1].endswith("…")


def test_wrap_caption_short_text_untouched():
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    assert wrap_caption(draw, "Hello", resolve_font(15), 400, 2) == ["Hello"]


def test_wrap_caption_empty():
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    assert wrap_caption(draw, "", resolve_font(15), 400, 2) == []


def test_wrap_caption_never_exceeds_max_lines():
    img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(img)
    long_text = "word " * 200
    assert len(wrap_caption(draw, long_text, resolve_font(15), 150, 3)) <= 3


# -------------------------------------------------------------------- composing


def test_compose_sheet_produces_a_valid_jpeg_of_planned_width(tmp_path):
    plan = plan_sheets(frame_width=720, frame_height=1280)
    tiles = [
        TileSpec(_frame(tmp_path, f"f{i}.jpg"), i, i * 1.5, caption=f"line {i}")
        for i in range(6)
    ]
    data, placeholders = compose_sheet(tiles, plan, title="adaptive 01")

    assert placeholders == 0
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.width == plan.sheet_width
    assert img.height > 0


def test_compose_sheet_trims_height_for_a_partial_last_page(tmp_path):
    plan = plan_sheets(frame_width=720, frame_height=1280)
    one_row = [TileSpec(_frame(tmp_path, f"a{i}.jpg"), i, float(i)) for i in range(plan.columns)]
    two_rows = [
        TileSpec(_frame(tmp_path, f"b{i}.jpg"), i, float(i)) for i in range(plan.columns + 1)
    ]

    short = Image.open(io.BytesIO(compose_sheet(one_row, plan)[0]))
    tall = Image.open(io.BytesIO(compose_sheet(two_rows, plan)[0]))
    assert tall.height > short.height


def test_missing_frame_renders_a_placeholder_not_an_exception(tmp_path):
    plan = plan_sheets(frame_width=720, frame_height=1280)
    tiles = [
        TileSpec(_frame(tmp_path, "good.jpg"), 0, 0.0),
        TileSpec(tmp_path / "does_not_exist.jpg", 1, 1.0),
        TileSpec(None, 2, 2.0),
    ]
    data, placeholders = compose_sheet(tiles, plan)
    assert placeholders == 2
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_corrupt_frame_renders_a_placeholder(tmp_path):
    """A truncated JPEG on disk must not lose the whole sheet."""
    good = _frame(tmp_path, "ok.jpg")
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")

    plan = plan_sheets(frame_width=720, frame_height=1280)
    data, placeholders = compose_sheet(
        [TileSpec(good, 0, 0.0), TileSpec(str(corrupt), 1, 1.0)], plan
    )
    assert placeholders == 1
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_letterboxing_never_distorts_a_mismatched_frame(tmp_path):
    """A landscape frame dropped into a portrait plan must be fitted, not
    stretched — we assert by checking the tile keeps background on the sides."""
    plan = plan_sheets(frame_width=720, frame_height=1280, columns=2, sheet_width=1000)
    odd = _frame(tmp_path, "wide.jpg", size=(1280, 720), color=(255, 0, 0))
    data, _ = compose_sheet([TileSpec(odd, 0, 0.0)], plan, theme_name="dark")

    img = Image.open(io.BytesIO(data)).convert("RGB")
    # Top-left of the tile area should still be tile background (letterbox),
    # not the red frame, because a 16:9 image cannot fill a 9:16 box.
    tile_top_left = img.getpixel((20 + 2, 18 + 54 + 2))
    assert tile_top_left != (255, 0, 0)


def test_both_themes_render():
    plan = plan_sheets(frame_width=720, frame_height=1280)
    for theme in THEMES:
        data, _ = compose_sheet([TileSpec(None, 0, 0.0)], plan, theme_name=theme)
        assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_unknown_theme_falls_back_to_dark():
    plan = plan_sheets(frame_width=720, frame_height=1280)
    data, _ = compose_sheet([TileSpec(None, 0, 0.0)], plan, theme_name="chartreuse")
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_quality_setting_affects_size(tmp_path):
    plan = plan_sheets(frame_width=720, frame_height=1280)
    tiles = [TileSpec(_frame(tmp_path, f"q{i}.jpg"), i, float(i)) for i in range(4)]
    low, _ = compose_sheet(tiles, plan, quality=50)
    high, _ = compose_sheet(tiles, plan, quality=95)
    assert len(high) > len(low)


def test_resolve_font_always_returns_something():
    font = resolve_font(18)
    assert font is not None
    bold = resolve_font(18, bold=True)
    assert bold is not None
