"""utils/manifest_compat — legacy datasets (v1, v2.0, v2.1) must normalize
into the current in-memory representation with warnings, never failures,
and the on-disk originals must never be touched."""
import copy
import json
from pathlib import Path

import pytest

from app.utils.manifest_compat import (
    detect_schema_version,
    normalize_frames,
    normalize_manifest,
    upgrade_posted_at_record,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_detect_schema_version():
    assert detect_schema_version({}) == (1, 0)
    assert detect_schema_version(None) == (1, 0)
    assert detect_schema_version({"dataset_schema_version": "2.0"}) == (2, 0)
    assert detect_schema_version({"dataset_schema_version": "2.2"}) == (2, 2)
    assert detect_schema_version({"dataset_schema_version": "garbage"}) == (1, 0)


@pytest.mark.parametrize("fixture", ["manifest_v1.json", "manifest_v2_0.json", "manifest_v2_1.json"])
def test_legacy_fixtures_normalize_without_errors(fixture):
    manifest = _load(fixture)
    normalized, warnings = normalize_manifest(manifest)
    assert isinstance(normalized, dict)
    # Warnings are compat notices, and every one carries a stable code.
    assert all("code" in w and "message" in w for w in warnings)


def test_normalize_does_not_mutate_input():
    manifest = _load("manifest_v2_0.json")
    before = copy.deepcopy(manifest)
    normalize_manifest(manifest)
    assert manifest == before


def test_v2_0_flat_dense_keys_synthesize_nested_block():
    normalized, warnings = normalize_manifest(_load("manifest_v2_0.json"))
    dense = normalized["extraction_params"]["opening_dense"]
    assert dense["enabled"] is True
    assert dense["duration_seconds"] == 8.0
    assert dense["interval_seconds"] == 0.25
    assert any(w["code"] == "legacy_dense_config_normalized" for w in warnings)


def test_naive_timestamps_normalized_with_warning():
    normalized, warnings = normalize_manifest(_load("manifest_v1.json"))
    assert normalized["processing"]["started_at"].endswith("+00:00")
    naive_warnings = [w for w in warnings if w["code"] == "naive_timestamp_normalized"]
    # v1 fixture has two naive stamps (started_at + manifest_generated_at).
    assert len(naive_warnings) == 2


def test_aware_timestamps_produce_no_warning():
    normalized, warnings = normalize_manifest(_load("manifest_v2_1.json"))
    codes = [w["code"] for w in warnings if w["code"] == "naive_timestamp_normalized"]
    # Only started_at is naive in the v2.1 fixture.
    assert len(codes) == 1
    assert normalized["identity"]["exported_at"] == "2026-07-24T09:00:00+00:00"


def test_legacy_posted_at_upgraded_to_precision_record():
    normalized, warnings = normalize_manifest(_load("manifest_v2_0.json"))
    posted_at = normalized["posting_context"]["posted_at"]
    assert posted_at["value"] == "2026-07-23"
    assert posted_at["precision"] == "date_only"
    assert posted_at["timezone"] is None
    assert posted_at["source"] == "yt_dlp"  # legacy "auto" mapped forward
    assert posted_at["entry_method"] == "automatic"
    assert any(w["code"] == "legacy_posted_at_normalized" for w in warnings)


def test_bare_string_posted_at_upgrades():
    record = upgrade_posted_at_record("2026-07-23")
    assert record["value"] == "2026-07-23"
    assert record["precision"] == "date_only"
    assert record["source"] == "yt_dlp"


def test_empty_posted_at_becomes_not_available():
    record = upgrade_posted_at_record(None)
    assert record["value"] is None
    assert record["status"] == "not_available"


def test_normalize_frames_backfills_v1_entries():
    v1_frames = [{"frame": 0, "timestamp": 1.5, "image": "0001.500.jpg", "mode": "adaptive", "scene_id": 2}]
    normalized = normalize_frames(v1_frames)
    assert normalized[0]["category"] == "adaptive"
    assert "description" in normalized[0]
    assert "scene 2" in normalized[0]["description"]
    # Input untouched.
    assert "category" not in v1_frames[0]


def test_corrupted_manifest_shapes_do_not_crash():
    # Wrong types everywhere — normalization must degrade gracefully.
    weird = {
        "dataset_schema_version": ["2", "2"],
        "extraction_params": "not a dict",
        "posting_context": {"posted_at": 12345},
        "processing": {"started_at": 999},
    }
    normalized, warnings = normalize_manifest(weird)
    assert isinstance(normalized, dict)
