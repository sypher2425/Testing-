"""StoryboardStep: the five sheet families, ordering, and failure tolerance.

These run against real JPEGs on disk (tiny ones) so the composition path is
genuinely exercised, not mocked.
"""
import json
from unittest.mock import patch

import pytest
from PIL import Image

from app.pipeline.steps.storyboards import StoryboardStep
from tests.test_pipeline_steps import make_ctx


def _write_frame(ctx, *parts, size=(360, 640), color=(40, 60, 120)) -> None:
    import io

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    ctx.storage.save_bytes(ctx.job_relative("frames", *parts), buf.getvalue())


def _build_job(
    tmp_path,
    *,
    adaptive=6,
    dense=4,
    segments=None,
    duration=30.0,
    options=None,
    frame_size=(360, 640),
):
    ctx, logs, _ = make_ctx(tmp_path, options=options or {})
    ctx.shared["mode"] = "adaptive"
    ctx.shared["original_filename"] = "clip.mp4"
    ctx.shared["video"] = {
        "duration_seconds": duration,
        "width": frame_size[0],
        "height": frame_size[1],
        "fps": 30.0,
        "has_audio": bool(segments),
    }
    ctx.shared["transcript"] = {
        "language": "en" if segments else None,
        "skipped": not segments,
        "segments": segments or [],
    }

    frames = []
    for i in range(adaptive):
        name = f"{i:04d}.000.jpg"
        _write_frame(ctx, name, size=frame_size, color=(30 + i * 12, 60, 120))
        frames.append(
            {
                "frame": i,
                "timestamp": float(i * (duration / max(adaptive, 1))),
                "image": name,
                "mode": "adaptive",
                "category": "adaptive",
                "scene_id": i // 2,
                "transcript_segment_index": None,
            }
        )
    for j in range(dense):
        name = f"{j:04d}.250.jpg"
        _write_frame(ctx, "opening_dense", name, size=frame_size, color=(120, 40, 40 + j * 20))
        frames.append(
            {
                "frame": adaptive + j,
                "timestamp": round(j * 0.25, 3),
                "image": f"opening_dense/{name}",
                "mode": "dense_interval",
                "category": "opening_dense",
                "transcript_segment_index": None,
            }
        )
    ctx.shared["frames"] = frames
    return ctx, logs


def _sheets(ctx) -> dict:
    return json.loads(ctx.storage.get(ctx.job_relative("storyboard_manifest.json")).read_bytes())


# --------------------------------------------------------------- happy paths


def test_builds_all_view_types_and_a_manifest(tmp_path):
    segments = [
        {"start": 0.0, "end": 5.0, "text": "Add a goal counter at the top"},
        {"start": 5.0, "end": 12.0, "text": "Make the car flip when it lands"},
        {"start": 12.0, "end": 20.0, "text": "Now make it 100x cooler"},
    ]
    ctx, _ = _build_job(tmp_path, adaptive=8, dense=6, segments=segments)

    StoryboardStep().run(ctx)

    manifest = _sheets(ctx)
    assert manifest["status"] == "success"
    types = set(manifest["types_built"])
    assert {"adaptive", "opening_dense", "timeline", "transcript", "key_moments"} <= types

    # Every declared sheet actually exists on disk and is a real JPEG.
    for sheet in manifest["storyboards"]:
        rel = sheet["file"].split("/")
        assert ctx.storage.exists(ctx.job_relative(*rel)), sheet["file"]
        img = Image.open(ctx.storage.get(ctx.job_relative(*rel)))
        assert img.format == "JPEG"
        assert img.width == manifest["layout"]["sheet_width"]


def test_tiles_are_chronological_within_every_sheet(tmp_path):
    """shared['frames'] is adaptive + opening_dense concatenated, so it jumps
    backwards in time — the sheets must not."""
    ctx, _ = _build_job(tmp_path, adaptive=10, dense=8)
    StoryboardStep().run(ctx)

    for sheet in _sheets(ctx)["storyboards"]:
        stamps = [t["timestampSeconds"] for t in sheet["frames"]]
        assert stamps == sorted(stamps), f"{sheet['file']} is out of order: {stamps}"


def test_adaptive_and_opening_sheets_use_only_their_own_frames(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=6, dense=5)
    StoryboardStep().run(ctx)

    by_type = {}
    for sheet in _sheets(ctx)["storyboards"]:
        by_type.setdefault(sheet["type"], []).extend(sheet["frames"])

    assert all(t["sourceFrame"].startswith("frames/opening_dense/") for t in by_type["opening_dense"])
    assert len(by_type["opening_dense"]) == 5
    assert all("opening_dense" not in t["sourceFrame"] for t in by_type["adaptive"])
    assert len(by_type["adaptive"]) == 6


def test_every_tile_has_a_frame_number_timestamp_and_source(tmp_path):
    """The AI-readability contract from the spec."""
    ctx, _ = _build_job(tmp_path, adaptive=5, dense=3)
    StoryboardStep().run(ctx)

    for sheet in _sheets(ctx)["storyboards"]:
        for tile in sheet["frames"]:
            assert isinstance(tile["frameNumber"], int)
            assert isinstance(tile["timestampSeconds"], float)
            assert tile["timestampLabel"].count(":") == 1
            assert tile["sourceFrame"].startswith("frames/")
            assert tile["tileIndex"] >= 1


def test_source_frames_referenced_by_the_manifest_all_exist(tmp_path):
    """The manifest's whole job is letting a consumer find the original."""
    ctx, _ = _build_job(tmp_path, adaptive=6, dense=4)
    StoryboardStep().run(ctx)

    for sheet in _sheets(ctx)["storyboards"]:
        for tile in sheet["frames"]:
            rel = tile["sourceFrame"].split("/")
            assert ctx.storage.exists(ctx.job_relative(*rel)), tile["sourceFrame"]


# ------------------------------------------------------------------ splitting


def test_large_frame_sets_split_across_numbered_sheets(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=45, dense=0, duration=90.0)
    StoryboardStep().run(ctx)

    manifest = _sheets(ctx)
    adaptive_sheets = [s for s in manifest["storyboards"] if s["type"] == "adaptive"]
    assert len(adaptive_sheets) > 1, "45 frames should not fit on one sheet"

    # Filenames are predictable and ordered.
    names = [s["file"] for s in adaptive_sheets]
    assert names == sorted(names)
    assert names[0].endswith("adaptive_storyboard_01.jpg")

    # No tile lost, none duplicated, order preserved across the split.
    tiles = [t for s in adaptive_sheets for t in s["frames"]]
    assert len(tiles) == 45
    stamps = [t["timestampSeconds"] for t in tiles]
    assert stamps == sorted(stamps)
    assert len(set(t["sourceFrame"] for t in tiles)) == 45


def test_key_moments_is_a_single_summary_sheet(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=40, dense=0, duration=80.0)
    StoryboardStep().run(ctx)

    key = [s for s in _sheets(ctx)["storyboards"] if s["type"] == "key_moments"]
    assert len(key) == 1
    assert key[0]["file"].endswith("key_moments_storyboard.jpg")
    assert len(key[0]["frames"]) <= 24


# ----------------------------------------------------------------- transcript


def test_transcript_sheet_has_one_tile_per_segment_with_full_text(tmp_path):
    segments = [
        {"start": 0.0, "end": 4.0, "text": "First instruction"},
        {"start": 4.0, "end": 9.0, "text": "Second instruction that is quite a lot longer so it must wrap"},
        {"start": 9.0, "end": 15.0, "text": "Third"},
    ]
    ctx, _ = _build_job(tmp_path, adaptive=10, dense=0, segments=segments, duration=15.0)
    StoryboardStep().run(ctx)

    tiles = [t for s in _sheets(ctx)["storyboards"] if s["type"] == "transcript" for t in s["frames"]]
    assert len(tiles) == len(segments)
    for i, tile in enumerate(tiles):
        seg = tile["transcriptSegment"]
        assert seg["index"] == i
        # Full untruncated text is preserved even if the drawn caption wrapped.
        assert seg["text"] == segments[i]["text"]
        # The chosen frame falls inside (or nearest) its segment's span.
        assert abs(tile["timestampSeconds"] - (segments[i]["start"] + segments[i]["end"]) / 2) <= 3.0


def test_captions_match_the_segment_active_at_that_timestamp(tmp_path):
    segments = [
        {"start": 0.0, "end": 10.0, "text": "EARLY LINE"},
        {"start": 20.0, "end": 30.0, "text": "LATE LINE"},
    ]
    ctx, _ = _build_job(tmp_path, adaptive=6, dense=0, segments=segments, duration=30.0)
    StoryboardStep().run(ctx)

    for sheet in _sheets(ctx)["storyboards"]:
        if sheet["type"] == "transcript":
            continue
        for tile in sheet["frames"]:
            seg = tile.get("transcriptSegment")
            if not seg:
                continue
            assert seg["start"] <= tile["timestampSeconds"] < seg["end"]


def test_works_with_no_transcript_at_all(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=6, dense=4, segments=None)
    StoryboardStep().run(ctx)

    manifest = _sheets(ctx)
    assert manifest["status"] == "success"
    # No transcript sheet, but the visual ones still built.
    assert "transcript" not in manifest["types_built"]
    assert "adaptive" in manifest["types_built"]
    for sheet in manifest["storyboards"]:
        for tile in sheet["frames"]:
            assert tile["transcriptSegment"] is None


# ------------------------------------------------------------- edge + failure


def test_single_frame_job_still_produces_a_sheet(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=1, dense=0, duration=2.0)
    StoryboardStep().run(ctx)
    manifest = _sheets(ctx)
    assert manifest["status"] == "success"
    assert len(manifest["storyboards"]) >= 1


def test_no_frames_skips_cleanly(tmp_path):
    ctx, logs = _build_job(tmp_path, adaptive=0, dense=0)
    ctx.shared["frames"] = []
    StoryboardStep().run(ctx)
    assert ctx.shared["storyboards"]["status"] == "skipped"
    assert ctx.shared["storyboards"]["reason"] == "no_frames"
    assert not ctx.storage.exists(ctx.job_relative("storyboard_manifest.json"))


def test_disabled_by_option(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=4, dense=0, options={"storyboard_enabled": False})
    StoryboardStep().run(ctx)
    assert ctx.shared["storyboards"]["status"] == "skipped"
    assert ctx.shared["storyboards"]["reason"] == "disabled_by_option"


def test_missing_frame_file_becomes_a_placeholder_not_a_failure(tmp_path):
    ctx, logs = _build_job(tmp_path, adaptive=5, dense=0)
    # Delete one frame from disk after the metadata was recorded.
    victim = ctx.storage.get(ctx.job_relative("frames", "0002.000.jpg"))
    victim.unlink()

    StoryboardStep().run(ctx)

    manifest = _sheets(ctx)
    assert manifest["status"] == "success"
    assert manifest["unavailable_frames"] >= 1
    assert any("placeholder" in msg.lower() for level, msg in logs if level == "warning")


def test_corrupt_frame_file_is_tolerated(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=4, dense=0)
    ctx.storage.save_bytes(ctx.job_relative("frames", "0001.000.jpg"), b"not a jpeg at all")
    StoryboardStep().run(ctx)
    assert _sheets(ctx)["status"] == "success"


def test_step_never_fails_the_job_when_composition_explodes(tmp_path):
    """A storyboard problem must not destroy a job whose frames and transcript
    are already on disk."""
    ctx, logs = _build_job(tmp_path, adaptive=4, dense=0)

    with patch(
        "app.pipeline.steps.storyboards.compose_sheet", side_effect=RuntimeError("boom")
    ):
        StoryboardStep().run(ctx)  # must not raise

    assert ctx.shared["storyboards"]["status"] == "extraction_failed"
    assert "RuntimeError" in ctx.shared["storyboards"]["reason"]
    assert any("Regenerate" in msg for level, msg in logs if level == "error")


# ---------------------------------------------------------------- orientation


def test_portrait_and_landscape_get_different_column_counts(tmp_path):
    portrait, _ = _build_job(tmp_path / "p", adaptive=12, dense=0, frame_size=(360, 640))
    StoryboardStep().run(portrait)
    landscape, _ = _build_job(tmp_path / "l", adaptive=12, dense=0, frame_size=(640, 360))
    StoryboardStep().run(landscape)

    assert _sheets(portrait)["layout"]["columns"] < _sheets(landscape)["layout"]["columns"]


def test_column_override_is_honoured(tmp_path):
    ctx, _ = _build_job(tmp_path, adaptive=8, dense=0, options={"storyboard_columns": 3})
    StoryboardStep().run(ctx)
    assert _sheets(ctx)["layout"]["columns"] == 3


def test_captions_can_be_turned_off(tmp_path):
    ctx, _ = _build_job(
        tmp_path, adaptive=4, dense=0, options={"storyboard_include_captions": False}
    )
    StoryboardStep().run(ctx)
    assert _sheets(ctx)["layout"]["captions"] is False
