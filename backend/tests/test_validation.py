"""Dataset validator: structural errors block export, everything else is a
warning that ships with the dataset (metadata/validation_report.json)."""
from app.utils.validation import validate_dataset


def _base_manifest(**overrides) -> dict:
    manifest = {
        "dataset_schema_version": "2.0",
        "original_filename": "clip.mp4",
        "video": {"duration_seconds": 30.0},
        "frame_counts": {"adaptive": 0, "opening_dense": 0, "key_events": 0},
        "extraction_params": {"opening_dense": {"enabled": True, "duration_seconds": 8.0, "interval_seconds": 0.25}},
        "performance": {},
    }
    manifest.update(overrides)
    return manifest


def _setup_job_dir(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "source").mkdir()
    (tmp_path / "frames").mkdir()
    return tmp_path


def test_clean_manifest_reports_success(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    report = validate_dataset(job_dir, _base_manifest())
    assert report.status == "success"
    assert report.errors == []


def test_missing_source_dir_is_a_warning(tmp_path):
    """The validator runs before manifest.json is written to disk — so it
    doesn't check that file itself. It DOES check the source/ directory
    when the manifest looks like a video-mode job."""
    report = validate_dataset(tmp_path, _base_manifest())
    assert any(w["code"] == "missing_source_dir" for w in report.warnings)


def test_missing_schema_version_is_a_warning(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest()
    del manifest["dataset_schema_version"]
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "missing_schema_version" for w in report.warnings)


def test_frame_count_mismatch_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "frames" / "0000.000.jpg").write_bytes(b"x")
    (job_dir / "frames" / "opening_dense").mkdir()
    (job_dir / "frames" / "opening_dense" / "0000.000.jpg").write_bytes(b"x")
    manifest = _base_manifest(frame_counts={"adaptive": 5, "opening_dense": 5, "key_events": 0})
    report = validate_dataset(job_dir, manifest)
    mismatches = [w for w in report.warnings if w["code"] == "frame_count_mismatch"]
    assert {m["category"] for m in mismatches} >= {"adaptive", "opening_dense"}
    for m in mismatches:
        assert m["on_disk"] == 1
        assert m["declared"] == 5


def test_dense_enabled_with_null_config_is_a_hard_error(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        extraction_params={"opening_dense": {"enabled": True, "duration_seconds": None, "interval_seconds": None}}
    )
    report = validate_dataset(job_dir, manifest)
    assert report.status == "error"
    assert any(e["code"] == "dense_config_null" for e in report.errors)


def test_frame_mode_category_contradiction_flagged(tmp_path):
    """The exact R1 bug: dense frames stored with mode=adaptive."""
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        _frames_for_validation=[
            {"timestamp": 0.5, "mode": "adaptive", "category": "opening_dense"},
        ]
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "incompatible_mode_category" for w in report.warnings)


def test_missing_performance_snapshot_flagged_for_url_jobs(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        performance={"source_url": "https://youtu.be/xyz", "platform": "youtube"},
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "missing_performance_snapshot" for w in report.warnings)


def test_silent_zero_metric_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        performance={
            "view_count": 0,
            "fields_status": {"view_count": {"status": "extraction_failed", "reason": "r"}},
        },
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "silent_zero" for w in report.warnings)


def test_invalid_status_value_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        performance={
            "view_count": None,
            "fields_status": {"view_count": {"status": "definitely_not_a_status"}},
        },
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "invalid_status" for w in report.warnings)


def test_identity_inconsistency_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(
        identity={
            "canonical_url": "https://www.tiktok.com/@x/video/111",
            "platform_post_id": "222",
        }
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "identity_inconsistent" for w in report.warnings)


def test_nested_dataset_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "accidental").mkdir()
    (job_dir / "accidental" / "manifest.json").write_text("{}")
    report = validate_dataset(job_dir, _base_manifest())
    assert any(w["code"] == "nested_dataset" for w in report.warnings)


def test_v1_manifest_without_v2_fields_only_warns_never_errors(tmp_path):
    """A schema-v1 manifest is a *legal* input to the validator — it should
    produce warnings (missing schema version, etc.) but never a hard error."""
    job_dir = _setup_job_dir(tmp_path)
    manifest = {
        "original_filename": "clip.mp4",
        "video": {"duration_seconds": 5.0},
        "performance": {},
    }
    report = validate_dataset(job_dir, manifest)
    assert report.status in ("success", "warning")
    assert report.errors == []
