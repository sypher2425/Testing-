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


# --------------------------------------------------------------- v2.2 checks


def _current_manifest(**overrides) -> dict:
    """A manifest declaring the current schema — contradictions are ERRORS."""
    return _base_manifest(dataset_schema_version="2.2", **overrides)


def test_dense_legacy_canonical_mismatch_is_error_on_current_schema(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        extraction_params={
            "opening_dense": {"enabled": True, "duration_seconds": 8.0, "interval_seconds": 0.25},
            "opening_dense_duration": 4.0,  # contradicts canonical
            "opening_dense_interval": 0.25,
        }
    )
    report = validate_dataset(job_dir, manifest)
    assert report.status == "error"
    assert any(e["code"] == "dense_legacy_canonical_mismatch" for e in report.errors)


def test_dense_legacy_null_while_enabled_is_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        extraction_params={
            "opening_dense": {"enabled": True, "duration_seconds": 8.0, "interval_seconds": 0.25},
            "opening_dense_enabled": True,
            "opening_dense_duration": None,  # must never be null while enabled
            "opening_dense_interval": 0.25,
        }
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "dense_legacy_canonical_mismatch" for e in report.errors)


def test_dense_mismatch_is_only_warning_on_legacy_schema(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _base_manifest(  # 2.0
        extraction_params={
            "opening_dense": {"enabled": True, "duration_seconds": 8.0, "interval_seconds": 0.25},
            "opening_dense_duration": 4.0,
        }
    )
    report = validate_dataset(job_dir, manifest)
    assert report.errors == []
    assert any(w["code"] == "dense_legacy_canonical_mismatch" for w in report.warnings)
    assert report.status == "warning"  # contradiction never yields a clean result


def test_dense_config_missing_when_only_flat_keys_exist(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        extraction_params={"opening_dense_enabled": True, "opening_dense_duration": 8.0}
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "dense_config_missing" for e in report.errors)


def test_frame_description_mismatch_flagged(tmp_path):
    from app.utils.frame_schema import describe_frame

    job_dir = _setup_job_dir(tmp_path)
    frame = {"frame": 0, "timestamp": 2.25, "image": "opening_dense/0002.250.jpg",
             "mode": "dense_interval", "category": "opening_dense",
             "description": "Extracted frame #0 at t=2.25s (adaptive mode)"}
    manifest = _current_manifest(_frames_for_validation=[frame])
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "dense_frame_described_as_adaptive" for e in report.errors)

    # And with the correct description the finding disappears.
    frame["description"] = describe_frame(frame)
    report = validate_dataset(job_dir, manifest)
    assert not any(
        e["code"] in ("dense_frame_described_as_adaptive", "frame_description_mismatch")
        for e in report.errors + report.warnings
    )


def test_files_description_disagreeing_with_metadata_flagged(tmp_path):
    from app.utils.frame_schema import describe_frame

    job_dir = _setup_job_dir(tmp_path)
    frame = {"frame": 0, "timestamp": 1.0, "image": "0001.000.jpg", "mode": "adaptive", "category": "adaptive"}
    frame["description"] = describe_frame(frame)
    manifest = _current_manifest(
        _frames_for_validation=[frame],
        files=[{"path": "frames/0001.000.jpg", "description": "Something else entirely", "size_bytes": 1}],
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "frame_description_mismatch" for e in report.errors)


def test_frame_category_count_mismatch_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        frame_counts={"adaptive": 3, "opening_dense": 0, "key_events": 0},
        _frames_for_validation=[
            {"frame": 0, "timestamp": 0.0, "image": "a.jpg", "mode": "adaptive", "category": "adaptive"}
        ],
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "frame_category_count_mismatch" for e in report.errors)


def test_total_frame_count_mismatch_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        frame_counts={"adaptive": 0, "opening_dense": 0, "key_events": 0},
        total_frame_count=62,
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "total_frame_count_mismatch" for e in report.errors)


def test_naive_timestamp_is_error_on_current_warning_on_legacy(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    processing = {"started_at": "2026-07-26T10:00:19.668895"}  # no offset

    report = validate_dataset(job_dir, _current_manifest(processing=processing))
    assert any(e["code"] == "naive_timestamp" for e in report.errors)

    report = validate_dataset(job_dir, _base_manifest(processing=processing))  # 2.0
    assert not any(e["code"] == "naive_timestamp" for e in report.errors)
    assert any(w["code"] == "naive_timestamp" for w in report.warnings)


def test_extraction_report_timestamps_checked(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        extraction_report=[{"stage": "probing", "started_at": "2026-01-01T00:00:00", "completed_at": None}]
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "naive_timestamp" for e in report.errors)


def test_posted_at_missing_precision_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        posting_context={"posted_at": {"value": "2026-07-23", "status": "success", "source": "yt_dlp"}}
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "posted_at_missing_precision" for e in report.errors)


def test_posted_at_invalid_and_mismatched_precision_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        posting_context={"posted_at": {"value": "2026-07-23", "status": "success", "precision": "very_precise"}}
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "invalid_temporal_precision" for w in report.warnings)

    manifest = _current_manifest(
        posting_context={"posted_at": {"value": "2026-07-23", "status": "success", "precision": "exact_datetime"}}
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "posted_at_precision_value_mismatch" for w in report.warnings)


def test_silence_totals_ambiguous_when_legacy_keys_lack_analysis(tmp_path):
    import json as _json

    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "content").mkdir()
    (job_dir / "content" / "audio.json").write_text(
        _json.dumps({"silence_or_render_wait_seconds": {"value": 17.36, "status": "success"}})
    )
    report = validate_dataset(job_dir, _current_manifest())
    assert any(e["code"] == "silence_totals_ambiguous" for e in report.errors)


def test_silence_listed_total_mismatch_flagged(tmp_path):
    import json as _json

    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "content").mkdir()
    (job_dir / "content" / "audio.json").write_text(
        _json.dumps(
            {
                "silence_analysis": {
                    "status": "success",
                    "total_non_narration_seconds": 17.36,
                    "total_scope": "all_gaps_within_voiceover_span",
                    "listed_periods_minimum_duration_seconds": 1.5,
                    "listed_periods_scope": "full_video_gaps_over_threshold",
                    "listed_periods": [{"start": 0.0, "end": 2.0, "duration_seconds": 2.0}],
                    "listed_periods_total_seconds": 10.84,  # doesn't match the 2.0 listed
                }
            }
        )
    )
    report = validate_dataset(job_dir, _current_manifest())
    assert any(e["code"] == "silence_listed_total_mismatch" for e in report.errors)


def test_silence_period_below_threshold_warned(tmp_path):
    import json as _json

    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "content").mkdir()
    (job_dir / "content" / "audio.json").write_text(
        _json.dumps(
            {
                "silence_analysis": {
                    "status": "success",
                    "total_non_narration_seconds": 0.5,
                    "total_scope": "all_gaps_within_voiceover_span",
                    "listed_periods_minimum_duration_seconds": 1.5,
                    "listed_periods_scope": "full_video_gaps_over_threshold",
                    "listed_periods": [{"start": 0.0, "end": 0.5, "duration_seconds": 0.5}],
                    "listed_periods_total_seconds": 0.5,
                }
            }
        )
    )
    report = validate_dataset(job_dir, _current_manifest())
    assert any(w["code"] == "silence_period_below_threshold" for w in report.warnings)


def test_deprecated_field_mismatch_flagged(tmp_path):
    import json as _json

    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "content").mkdir()
    (job_dir / "content" / "audio.json").write_text(
        _json.dumps(
            {
                "voiceover_span_seconds": {"value": 40.0, "status": "success"},
                "voiceover_duration_seconds": {"value": 35.0, "status": "success"},  # alias disagrees
            }
        )
    )
    report = validate_dataset(job_dir, _current_manifest())
    assert any(e["code"] == "deprecated_field_mismatch" for e in report.errors)


def test_frame_count_alias_mismatch_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(frame_count=99, frame_counts={"adaptive": 30, "opening_dense": 0, "key_events": 0})
    report = validate_dataset(job_dir, manifest)
    assert any(
        e["code"] == "deprecated_field_mismatch" and e.get("field") == "frame_count" for e in report.errors
    )


def test_deprecated_source_label_warned_only_on_current_schema(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    posting_context = {"platform": {"value": "tiktok", "status": "success", "source": "auto"}}

    report = validate_dataset(job_dir, _current_manifest(posting_context=posting_context))
    assert any(w["code"] == "deprecated_source_label" for w in report.warnings)

    report = validate_dataset(job_dir, _base_manifest(posting_context=posting_context))  # 2.0: silent
    assert not any(w["code"] == "deprecated_source_label" for w in report.warnings)


def test_invalid_entry_method_flagged(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        performance={
            "view_count": 5,
            "fields_status": {"view_count": {"status": "success", "source": "yt_dlp", "entry_method": "telepathy"}},
        }
    )
    report = validate_dataset(job_dir, manifest)
    assert any(w["code"] == "invalid_entry_method" for w in report.warnings)


def test_rate_precision_labels_validated(tmp_path):
    import json as _json

    job_dir = _setup_job_dir(tmp_path)
    (job_dir / "analytics").mkdir()
    (job_dir / "analytics" / "performance.json").write_text(
        _json.dumps(
            {
                "metrics": {},
                "rates": {
                    "like_rate": {"value": 0.05, "status": "calculated", "precision": "derived_from_rounded"},
                    "share_rate": {"value": 0.01, "status": "calculated", "precision": "made_up_label"},
                },
            }
        )
    )
    report = validate_dataset(job_dir, _current_manifest())
    flagged = [w for w in report.warnings if w["code"] == "invalid_precision"]
    assert [w["metric"] for w in flagged] == ["share_rate"]  # derived_from_rounded is legal


def test_incompatible_mode_category_escalates_to_error_on_current(tmp_path):
    job_dir = _setup_job_dir(tmp_path)
    manifest = _current_manifest(
        _frames_for_validation=[{"timestamp": 0.5, "mode": "adaptive", "category": "opening_dense"}]
    )
    report = validate_dataset(job_dir, manifest)
    assert any(e["code"] == "incompatible_mode_category" for e in report.errors)
