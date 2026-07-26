"""Pre-export dataset validator.

Runs at the end of GenerateMetadataStep, writes
metadata/validation_report.json into the job dir, and appends warnings to
the job log. Structural errors (missing manifest, missing source video)
raise PipelineFailedError; everything else is a warning that ships with
the dataset so downstream consumers can see it.

Validation is intentionally advisory-by-default. This module never
mutates the manifest — it only observes and reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.utils import precision, status as st
from app.utils.frame_schema import CATEGORIES, MODES, VALID_COMBOS

STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


@dataclass
class ValidationReport:
    status: str
    warnings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    validated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "validation_status": self.status,
            "warnings": self.warnings,
            "errors": self.errors,
            "validated_at": self.validated_at,
        }


def _warn(report: ValidationReport, code: str, message: str, **extra) -> None:
    report.warnings.append({"code": code, "message": message, **extra})


def _err(report: ValidationReport, code: str, message: str, **extra) -> None:
    report.errors.append({"code": code, "message": message, **extra})


def validate_dataset(job_dir: Path, manifest: dict) -> ValidationReport:
    report = ValidationReport(status=STATUS_SUCCESS)
    report.validated_at = datetime.now(timezone.utc).isoformat()

    _check_required_files(job_dir, manifest, report)
    _check_schema_version(manifest, report)
    _check_frame_counts(job_dir, manifest, report)
    _check_dense_config(manifest, report)
    _check_frame_mode_category(manifest, report)
    _check_performance_snapshot(manifest, report)
    _check_metrics_and_rates(manifest, report)
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


def _check_frame_counts(job_dir: Path, manifest: dict, report: ValidationReport) -> None:
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
            _warn(
                report,
                "frame_count_mismatch",
                f"Manifest declares {declared} {category} frames but found {disk_count} on disk",
                category=category,
                declared=declared,
                on_disk=disk_count,
            )


def _check_dense_config(manifest: dict, report: ValidationReport) -> None:
    params = manifest.get("extraction_params") or {}
    dense = params.get("opening_dense")
    if not isinstance(dense, dict):
        return
    if dense.get("enabled") and (dense.get("duration_seconds") in (None, 0) or dense.get("interval_seconds") in (None, 0)):
        _err(
            report,
            "dense_config_null",
            "opening_dense is enabled but duration_seconds/interval_seconds are null or zero",
        )


def _check_frame_mode_category(manifest: dict, report: ValidationReport) -> None:
    for frame in _all_frame_entries(manifest):
        mode = frame.get("mode")
        category = frame.get("category")
        if category is None:
            # v1 frames have no category — skip; a separate v1-compat check covers that.
            continue
        if mode not in MODES:
            _warn(report, "invalid_frame_mode", f"Frame at t={frame.get('timestamp')} has unknown mode {mode!r}")
            continue
        if category not in CATEGORIES:
            _warn(report, "invalid_frame_category", f"Frame at t={frame.get('timestamp')} has unknown category {category!r}")
            continue
        if (mode, category) not in VALID_COMBOS:
            _warn(
                report,
                "incompatible_mode_category",
                f"Frame at t={frame.get('timestamp')} has incompatible (mode, category)=({mode!r}, {category!r})",
                mode=mode,
                category=category,
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
        if precision_value is not None and precision_value not in precision.ALL_PRECISIONS:
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


def _all_frame_entries(manifest: dict) -> list[dict]:
    # Frames live in metadata/frames.json on disk; the manifest itself doesn't
    # embed them. The validator receives a shared context dict — callers can
    # inject `frames` via manifest["_frames_for_validation"] to avoid a re-read.
    injected = manifest.get("_frames_for_validation")
    if isinstance(injected, list):
        return injected
    return []
