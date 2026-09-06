"""Pre-export dataset validator.

Runs at the end of GenerateMetadataStep, writes
metadata/validation_report.json into the job dir, and appends warnings to
the job log. Validation is report-only: it never blocks the pipeline and
never mutates the manifest — it observes and reports.

Severity policy (schema v2.2): every finding has a stable code. Structural
contradictions — declared values disagreeing with each other or with the
files on disk — are ERRORS on datasets this pipeline just generated
(declared schema >= 2.2) and WARNINGS on legacy datasets, which must keep
loading. Either way the validation status is not "success", so a
contradictory dataset can never present a clean report.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.utils import precision, status as st, timestamps as ts_util
from app.utils.frame_schema import (
    CATEGORIES,
    CATEGORY_OPENING_DENSE,
    MODES,
    VALID_COMBOS,
    describe_frame,
)
from app.utils.manifest_compat import detect_schema_version
from app.utils.timestamps import now_utc_iso

STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

# Silence totals are rounded to 2 decimals at generation time.
_SILENCE_TOLERANCE_SECONDS = 0.05


@dataclass
class ValidationReport:
    status: str
    warnings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    validated_at: str = ""
    schema_version_checked: str = ""

    def to_dict(self) -> dict:
        return {
            "validation_status": self.status,
            "warnings": self.warnings,
            "errors": self.errors,
            "validated_at": self.validated_at,
            "schema_version_checked": self.schema_version_checked,
        }


def _warn(report: ValidationReport, code: str, message: str, **extra) -> None:
    report.warnings.append({"code": code, "message": message, **extra})


def _err(report: ValidationReport, code: str, message: str, **extra) -> None:
    report.errors.append({"code": code, "message": message, **extra})


class _Checker:
    """Carries the severity policy so each check just states its finding."""

    def __init__(self, report: ValidationReport, is_current_schema: bool):
        self.report = report
        self.is_current_schema = is_current_schema

    def contradiction(self, code: str, message: str, **extra) -> None:
        """A structural contradiction: error on >=2.2, warning on legacy."""
        if self.is_current_schema:
            _err(self.report, code, message, **extra)
        else:
            _warn(self.report, code, message, **extra)

    def warn(self, code: str, message: str, **extra) -> None:
        _warn(self.report, code, message, **extra)


def validate_dataset(job_dir: Path, manifest: dict) -> ValidationReport:
    report = ValidationReport(status=STATUS_SUCCESS)
    report.validated_at = now_utc_iso()
    version = detect_schema_version(manifest)
    report.schema_version_checked = f"{version[0]}.{version[1]}"
    check = _Checker(report, is_current_schema=version >= (2, 2))

    _check_required_files(job_dir, manifest, report)
    _check_schema_version(manifest, report)
    _check_frame_counts(job_dir, manifest, check)
    _check_frame_metadata_counts(manifest, check)
    _check_dense_config(manifest, check)
    _check_frame_mode_category(manifest, check)
    _check_frame_descriptions(manifest, check)
    _check_performance_snapshot(manifest, report)
    _check_metrics_and_rates(manifest, report)
    _check_rate_precisions(job_dir, report)
    _check_timestamps(manifest, check)
    _check_posted_at(manifest, check)
    _check_silence(job_dir, check)
    _check_deprecated_fields(job_dir, manifest, check)
    _check_source_labels(manifest, check)
    _check_identity(manifest, report)
    _check_audio_consistency(manifest, report)
    _check_no_nested_dataset(job_dir, report)

    if report.errors:
        report.status = STATUS_ERROR
    elif report.warnings:
        report.status = STATUS_WARNING
    return report


# ------------------------------------------------------------------- checks


def _check_required_files(job_dir: Path, manifest: dict, report: ValidationReport) -> None:
    # NB: manifest.json existence is intentionally NOT checked here — this
    # validator runs from GenerateMetadataStep just before manifest.json is
    # written, so the file wouldn't exist yet during a normal pipeline run.
    # A future "re-validate a completed dataset" entry point can check that
    # separately.
    stored = manifest.get("original_filename")
    for legacy_file, code in (("source", "missing_source_dir"),):
        if not (job_dir / legacy_file).is_dir():
            # Research-mode jobs don't have source/, that's fine.
            if manifest.get("dataset_schema_version") and not stored:
                continue
            _warn(report, code, f"Expected '{legacy_file}/' directory in job dir")


def _check_schema_version(manifest: dict, report: ValidationReport) -> None:
    if not manifest.get("dataset_schema_version"):
        _warn(report, "missing_schema_version", "dataset_schema_version is not set")


def _check_frame_counts(job_dir: Path, manifest: dict, check: _Checker) -> None:
    """Declared frame_counts vs the files actually exported to disk."""
    counts_declared = manifest.get("frame_counts") or {}
    if not counts_declared:
        return
    frames_dir = job_dir / "frames"
    if not frames_dir.is_dir():
        return

    # adaptive frames are flat files in frames/ (excluding subdirectories)
    on_disk = {
        "adaptive": sum(1 for p in frames_dir.iterdir() if p.is_file()),
        "opening_dense": sum(1 for p in (frames_dir / "opening_dense").glob("*") if p.is_file())
        if (frames_dir / "opening_dense").is_dir()
        else 0,
        "key_events": sum(1 for p in (frames_dir / "key_events").glob("*") if p.is_file())
        if (frames_dir / "key_events").is_dir()
        else 0,
    }
    for category, disk_count in on_disk.items():
        declared = counts_declared.get(category, 0)
        if declared != disk_count:
            check.contradiction(
                "frame_count_mismatch",
                f"Manifest declares {declared} {category} frames but found {disk_count} on disk",
                category=category,
                declared=declared,
                on_disk=disk_count,
            )

    total_declared = manifest.get("total_frame_count")
    if total_declared is not None:
        counted = sum(counts_declared.values())
        if total_declared != counted:
            check.contradiction(
                "total_frame_count_mismatch",
                f"total_frame_count is {total_declared} but frame_counts sum to {counted}",
                declared=total_declared,
                computed=counted,
            )


def _check_frame_metadata_counts(manifest: dict, check: _Checker) -> None:
    """Declared frame_counts vs the categories in the frame metadata itself."""
    counts_declared = manifest.get("frame_counts") or {}
    frames = _all_frame_entries(manifest)
    if not counts_declared or not frames:
        return
    from_metadata = {"adaptive": 0, "opening_dense": 0, "key_events": 0}
    for frame in frames:
        category = frame.get("category", "adaptive")
        if category == CATEGORY_OPENING_DENSE:
            from_metadata["opening_dense"] += 1
        elif isinstance(category, str) and category.startswith("key_event"):
            from_metadata["key_events"] += 1
        else:
            from_metadata["adaptive"] += 1
    for category, meta_count in from_metadata.items():
        declared = counts_declared.get(category, 0)
        if declared != meta_count:
            check.contradiction(
                "frame_category_count_mismatch",
                f"frame_counts declares {declared} {category} frames but frame metadata contains {meta_count}",
                category=category,
                declared=declared,
                in_metadata=meta_count,
            )


def _check_dense_config(manifest: dict, check: _Checker) -> None:
    params = manifest.get("extraction_params") or {}
    dense = params.get("opening_dense")
    flat = {
        "enabled": params.get("opening_dense_enabled"),
        "duration_seconds": params.get("opening_dense_duration"),
        "interval_seconds": params.get("opening_dense_interval"),
    }
    has_flat = any(value is not None for value in flat.values())

    if not isinstance(dense, dict):
        if flat["enabled"] or flat["duration_seconds"] is not None or flat["interval_seconds"] is not None:
            check.contradiction(
                "dense_config_missing",
                "Deprecated flat opening_dense_* keys are present but the canonical "
                "extraction_params.opening_dense block is missing",
            )
        return

    if dense.get("enabled") and (
        dense.get("duration_seconds") in (None, 0) or dense.get("interval_seconds") in (None, 0)
    ):
        # Always an error: a dataset generated with dense frames enabled but
        # no recorded duration/interval is unreconstructable.
        _err(
            check.report,
            "dense_config_null",
            "opening_dense is enabled but duration_seconds/interval_seconds are null or zero",
        )

    if has_flat:
        mismatches = []
        if flat["enabled"] is not None and bool(flat["enabled"]) != bool(dense.get("enabled")):
            mismatches.append("enabled")
        for key in ("duration_seconds", "interval_seconds"):
            if flat[key] is not None and flat[key] != dense.get(key):
                mismatches.append(key)
        # Flat keys must never be null while dense extraction is enabled.
        if dense.get("enabled"):
            for flat_key, canonical_key in (
                ("opening_dense_duration", "duration_seconds"),
                ("opening_dense_interval", "interval_seconds"),
            ):
                if flat_key in params and params[flat_key] is None and dense.get(canonical_key) is not None:
                    mismatches.append(canonical_key)
        if mismatches:
            check.contradiction(
                "dense_legacy_canonical_mismatch",
                "Deprecated flat opening_dense_* keys disagree with the canonical "
                f"extraction_params.opening_dense block ({', '.join(sorted(set(mismatches)))})",
                fields=sorted(set(mismatches)),
            )


def _check_frame_mode_category(manifest: dict, check: _Checker) -> None:
    for frame in _all_frame_entries(manifest):
        mode = frame.get("mode")
        category = frame.get("category")
        if category is None:
            # v1 frames have no category — skip; a separate v1-compat check covers that.
            continue
        if mode not in MODES:
            check.warn("invalid_frame_mode", f"Frame at t={frame.get('timestamp')} has unknown mode {mode!r}")
            continue
        if category not in CATEGORIES:
            check.warn("invalid_frame_category", f"Frame at t={frame.get('timestamp')} has unknown category {category!r}")
            continue
        if (mode, category) not in VALID_COMBOS:
            check.contradiction(
                "incompatible_mode_category",
                f"Frame at t={frame.get('timestamp')} has incompatible (mode, category)=({mode!r}, {category!r})",
                mode=mode,
                category=category,
            )


def _check_frame_descriptions(manifest: dict, check: _Checker) -> None:
    """Descriptions must agree with the frame's own metadata. Generator and
    validator share describe_frame, so on current datasets this is exact
    string equality; for legacy prose we fall back to the one heuristic that
    catches the original bug (a dense frame described as adaptive)."""
    frames = _all_frame_entries(manifest)
    by_image: dict[str, dict] = {}
    for frame in frames:
        image = frame.get("image")
        if isinstance(image, str):
            by_image[image] = frame
        description = frame.get("description")
        if description is None:
            continue
        expected = describe_frame(frame)
        if description != expected:
            if frame.get("category") == CATEGORY_OPENING_DENSE and "adaptive" in description:
                check.contradiction(
                    "dense_frame_described_as_adaptive",
                    f"Opening-dense frame at t={frame.get('timestamp')} is described as adaptive: {description!r}",
                )
            else:
                check.contradiction(
                    "frame_description_mismatch",
                    f"Frame at t={frame.get('timestamp')} has description {description!r} "
                    f"but its metadata describes {expected!r}",
                )

    # The manifest files[] prose must match the same per-frame description.
    for entry in manifest.get("files") or []:
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("frames/") or path == "frames/":
            continue
        image = path[len("frames/"):]
        frame = by_image.get(image)
        if frame is None:
            continue
        description = entry.get("description")
        expected = frame.get("description") or describe_frame(frame)
        if description == expected:
            continue
        if frame.get("category") == CATEGORY_OPENING_DENSE and isinstance(description, str) and "adaptive" in description:
            check.contradiction(
                "dense_frame_described_as_adaptive",
                f"files[] describes opening-dense frame {image!r} as adaptive: {description!r}",
            )
        else:
            check.contradiction(
                "frame_description_mismatch",
                f"files[] description for {image!r} disagrees with the frame's metadata",
            )


def _check_performance_snapshot(manifest: dict, report: ValidationReport) -> None:
    performance = manifest.get("performance") or {}
    if not performance.get("source_url") and performance.get("platform") in (None, "manual"):
        return  # no URL-based extraction happened; snapshot doesn't apply
    snapshot = manifest.get("performance_snapshot") or {}
    if not snapshot.get("fetched_at"):
        _warn(
            report,
            "missing_performance_snapshot",
            "performance has a source_url but performance_snapshot.fetched_at is missing",
        )


def _check_metrics_and_rates(manifest: dict, report: ValidationReport) -> None:
    # Zero-with-non-success is the "silent zero" bug pattern we're guarding
    # against everywhere. Also verify each rate references its inputs.
    performance = manifest.get("performance") or {}
    fields_status = performance.get("fields_status") or {}
    for key, status_record in fields_status.items():
        value = performance.get(key)
        if value == 0 and status_record.get("status") != st.SUCCESS:
            _warn(
                report,
                "silent_zero",
                f"Metric {key!r} has value 0 but status is {status_record.get('status')!r} — should be null",
                metric=key,
            )
        precision_value = status_record.get("precision")
        if precision_value is not None and not precision.is_valid_precision_label(precision_value):
            _warn(
                report,
                "invalid_precision",
                f"Metric {key!r} has unknown precision {precision_value!r}",
                metric=key,
            )
        status_value = status_record.get("status")
        if status_value is not None and status_value not in st.ALL_STATUSES:
            _warn(
                report,
                "invalid_status",
                f"Metric {key!r} has unknown status {status_value!r}",
                metric=key,
            )
        entry_method = status_record.get("entry_method")
        if entry_method is not None and entry_method not in st.ALL_ENTRY_METHODS:
            _warn(
                report,
                "invalid_entry_method",
                f"Metric {key!r} has unknown entry_method {entry_method!r}",
                metric=key,
            )


def _check_rate_precisions(job_dir: Path, report: ValidationReport) -> None:
    """Rates live in analytics/performance.json and carry derived_from_*
    precision labels the base enum check used to skip entirely."""
    engagement = _load_json(job_dir / "analytics" / "performance.json")
    for rate_name, rate in (engagement.get("rates") or {}).items():
        if not isinstance(rate, dict):
            continue
        label = rate.get("precision")
        if label is not None and not precision.is_valid_precision_label(label):
            _warn(
                report,
                "invalid_precision",
                f"Rate {rate_name!r} has unknown precision {label!r}",
                metric=rate_name,
            )


def _check_timestamps(manifest: dict, check: _Checker) -> None:
    """Every generated processing timestamp must carry an explicit UTC
    offset. Naive stamps in legacy datasets warn; in fresh ones they error."""
    stamp_sites: list[tuple[str, object]] = []
    for section, key in (
        ("processing", "started_at"),
        ("processing", "manifest_generated_at"),
        ("identity", "exported_at"),
        ("performance_snapshot", "fetched_at"),
        ("comments", "attempted_at"),
        ("validation", "validated_at"),
    ):
        block = manifest.get(section)
        if isinstance(block, dict):
            stamp_sites.append((f"{section}.{key}", block.get(key)))
    for i, stage in enumerate(manifest.get("extraction_report") or []):
        if isinstance(stage, dict):
            name = stage.get("stage", i)
            stamp_sites.append((f"extraction_report[{name}].started_at", stage.get("started_at")))
            stamp_sites.append((f"extraction_report[{name}].completed_at", stage.get("completed_at")))

    for label, stamp in stamp_sites:
        if isinstance(stamp, str) and not ts_util.is_aware_iso(stamp):
            check.contradiction(
                "naive_timestamp",
                f"{label} ({stamp!r}) has no timezone offset; generated timestamps must be UTC-explicit",
                field=label,
            )


def _check_posted_at(manifest: dict, check: _Checker) -> None:
    posting_context = manifest.get("posting_context")
    if not isinstance(posting_context, dict):
        return
    posted_at = posting_context.get("posted_at")
    if not isinstance(posted_at, dict) or posted_at.get("value") in (None, ""):
        return
    declared = posted_at.get("precision")
    if declared is None:
        check.contradiction(
            "posted_at_missing_precision",
            "posting_context.posted_at has a value but no declared temporal precision",
        )
        return
    if declared not in ts_util.TEMPORAL_PRECISIONS:
        check.warn(
            "invalid_temporal_precision",
            f"posted_at precision {declared!r} is not in the temporal precision vocabulary",
        )
        return
    inferred = ts_util.infer_date_precision(posted_at.get("value"))
    # A date-shaped value claiming a finer precision than the string shows is
    # a contradiction the value itself disproves.
    if declared in (ts_util.EXACT_DATETIME, ts_util.MINUTE, ts_util.HOUR) and inferred in (
        ts_util.DATE_ONLY,
        ts_util.MONTH_ONLY,
    ):
        check.warn(
            "posted_at_precision_value_mismatch",
            f"posted_at declares precision {declared!r} but the value {posted_at.get('value')!r} "
            f"only supports {inferred!r}",
        )
    elif declared == ts_util.DATE_ONLY and inferred in (ts_util.EXACT_DATETIME, ts_util.MINUTE):
        check.warn(
            "posted_at_precision_value_mismatch",
            f"posted_at declares precision 'date_only' but the value {posted_at.get('value')!r} "
            "carries a time component",
        )


def _check_silence(job_dir: Path, check: _Checker) -> None:
    audio = _load_json(job_dir / "content" / "audio.json")
    if not audio:
        return
    analysis = audio.get("silence_analysis")
    has_legacy = "silence_or_render_wait_seconds" in audio or "silence_periods" in audio
    if not isinstance(analysis, dict):
        if has_legacy:
            check.contradiction(
                "silence_totals_ambiguous",
                "audio.json has legacy silence totals but no silence_analysis block "
                "declaring their scope and threshold",
            )
        return
    if analysis.get("status") != st.SUCCESS:
        return

    periods = analysis.get("listed_periods") or []
    threshold = analysis.get("listed_periods_minimum_duration_seconds")
    computed_total = 0.0
    for period in periods:
        start, end = period.get("start"), period.get("end")
        duration = period.get("duration_seconds")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and isinstance(duration, (int, float)):
            if abs((end - start) - duration) > _SILENCE_TOLERANCE_SECONDS:
                check.contradiction(
                    "silence_listed_total_mismatch",
                    f"Silence period {start}-{end}s declares duration {duration}s "
                    f"but end-start is {round(end - start, 2)}s",
                )
            computed_total += duration
            if isinstance(threshold, (int, float)) and duration + _SILENCE_TOLERANCE_SECONDS < threshold:
                check.warn(
                    "silence_period_below_threshold",
                    f"Silence period {start}-{end}s ({duration}s) is shorter than the declared "
                    f"minimum of {threshold}s",
                )
    declared_total = analysis.get("listed_periods_total_seconds")
    if isinstance(declared_total, (int, float)) and abs(declared_total - computed_total) > _SILENCE_TOLERANCE_SECONDS:
        check.contradiction(
            "silence_listed_total_mismatch",
            f"listed_periods_total_seconds is {declared_total} but the listed periods sum to "
            f"{round(computed_total, 2)}",
        )


def _check_deprecated_fields(job_dir: Path, manifest: dict, check: _Checker) -> None:
    """Deprecated aliases must mirror their canonical replacements exactly."""
    # manifest: frame_count (legacy, adaptive-only) vs frame_counts.adaptive
    frame_count = manifest.get("frame_count")
    counts = manifest.get("frame_counts") or {}
    if frame_count is not None and "adaptive" in counts and frame_count != counts["adaptive"]:
        check.contradiction(
            "deprecated_field_mismatch",
            f"frame_count ({frame_count}) disagrees with frame_counts.adaptive ({counts['adaptive']})",
            field="frame_count",
        )

    audio = _load_json(job_dir / "content" / "audio.json")
    if not audio:
        return

    def value_of(key: str):
        record = audio.get(key)
        return record.get("value") if isinstance(record, dict) else None

    for deprecated, canonical in (
        ("voiceover_duration_seconds", "voiceover_span_seconds"),
        ("overall_wpm", "overall_video_wpm"),
    ):
        old, new = value_of(deprecated), value_of(canonical)
        if old is not None and new is not None and old != new:
            check.contradiction(
                "deprecated_field_mismatch",
                f"audio.json {deprecated} ({old}) disagrees with {canonical} ({new})",
                field=deprecated,
            )

    analysis = audio.get("silence_analysis")
    if isinstance(analysis, dict):
        legacy_total = value_of("silence_or_render_wait_seconds")
        canonical_total = analysis.get("total_non_narration_seconds")
        if (
            isinstance(legacy_total, (int, float))
            and isinstance(canonical_total, (int, float))
            and abs(legacy_total - canonical_total) > _SILENCE_TOLERANCE_SECONDS
        ):
            check.contradiction(
                "deprecated_field_mismatch",
                f"audio.json silence_or_render_wait_seconds ({legacy_total}) disagrees with "
                f"silence_analysis.total_non_narration_seconds ({canonical_total})",
                field="silence_or_render_wait_seconds",
            )


def _check_source_labels(manifest: dict, check: _Checker) -> None:
    """Schema >=2.2 must not write "auto" as a source — provenance and entry
    method are separate fields now. Legacy datasets are exempt (silent)."""
    if not check.is_current_schema:
        return

    found: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get("source") == st.SOURCE_AUTO and "status" in node:
                found.append(path)
            for key, child in node.items():
                if key == "fields_from":  # legacy auto/manual UI contract, exempt
                    continue
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")

    walk(manifest, "")
    for path in found[:10]:
        check.warn(
            "deprecated_source_label",
            f"{path} uses deprecated source \"auto\"; use yt_dlp/user/computed plus entry_method",
            field=path,
        )


def _check_identity(manifest: dict, report: ValidationReport) -> None:
    identity = manifest.get("identity") or {}
    canonical = identity.get("canonical_url")
    post_id = identity.get("platform_post_id")
    if canonical and post_id and post_id not in canonical:
        _warn(
            report,
            "identity_inconsistent",
            f"canonical_url {canonical!r} does not contain platform_post_id {post_id!r}",
        )


def _check_audio_consistency(manifest: dict, report: ValidationReport) -> None:
    """Audio consistency lives in content/audio.json — but for a fast check
    we also look at the manifest's analysis_summary WPM values."""
    video = manifest.get("video") or {}
    duration = video.get("duration_seconds")
    summary = manifest.get("analysis_summary") or {}
    overall = (summary.get("overall_video_wpm") or summary.get("overall_wpm") or {}).get("value")
    active = (summary.get("active_narration_wpm") or {}).get("value")
    if overall and active and duration and overall > active:
        _warn(
            report,
            "audio_wpm_ordering",
            "overall_video_wpm > active_narration_wpm — active narration should always be at least as fast",
        )


def _check_no_nested_dataset(job_dir: Path, report: ValidationReport) -> None:
    """The zip step already excludes these; the validator raises visibility
    at manifest-generation time so the report captures them too."""
    for entry in job_dir.rglob("manifest.json"):
        if entry.parent == job_dir:
            continue
        _warn(
            report,
            "nested_dataset",
            f"Found a nested manifest.json at {entry.relative_to(job_dir)} — it will be excluded from the ZIP",
            path=str(entry.relative_to(job_dir)),
        )


# ------------------------------------------------------------------- helpers


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _all_frame_entries(manifest: dict) -> list[dict]:
    # Frames live in metadata/frames.json on disk; the manifest itself doesn't
    # embed them. The validator receives a shared context dict — callers can
    # inject `frames` via manifest["_frames_for_validation"] to avoid a re-read.
    injected = manifest.get("_frames_for_validation")
    if isinstance(injected, list):
        return injected
    return []
