"""Budgeted frame extraction with FFmpeg decoding and original source PTS.

Adaptive selection combines timeline coverage with cheap visual/transcript
change signals. Dense requests are decoded in batches; every-frame mode
iterates actual source frames, including variable frame-rate input.
"""
import bisect
import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.ffmpeg import FFmpegError, extract_frame_at
from app.utils.frame_decode import DecodedFrame, decode_frames
from app.utils.filenames import frame_filename


@dataclass
class Selection:
    timestamp: float
    scene_id: int | None


def _hwaccel(settings) -> str:
    value = getattr(settings, "FFMPEG_HWACCEL", "auto")
    return value if isinstance(value, str) else "auto"


def _resolve_range(ctx: PipelineContext, duration: float) -> tuple[float, float]:
    start = max(0.0, float(ctx.options.get("range_start_seconds") or 0.0))
    requested_end = ctx.options.get("range_end_seconds")
    end = min(duration, float(requested_end)) if requested_end is not None else duration
    if duration > 0 and (start >= duration or end <= start):
        raise PipelineFailedError("invalid_frame_range", "The frame range must overlap the video and end after it starts.")
    return start, max(start, end)


def _frame_budget(ctx: PipelineContext, settings) -> int:
    return max(1, min(int(settings.MAX_FRAMES), int(ctx.options.get("frame_budget") or 2000)))


def _detect_scenes(source_path: str, ctx: PipelineContext) -> list[tuple[float, float, float]]:
    """Cheap visual-change candidates, decoded by FFmpeg for AV1 compatibility.

These are scored windows rather than claims that a semantic event happened.
Coverage sampling remains independent so a quiet slide is never excluded
merely because a later part of the video contains more motion.
    """
    import cv2
    import numpy as np

    settings = get_settings()
    duration = float(ctx.shared.get("video", {}).get("duration_seconds") or 0)
    start, end = _resolve_range(ctx, duration)
    profile = ctx.options.get("processing_profile", "balanced")
    scan_fps = {"fast": 0.5, "balanced": 2.0, "detailed": 4.0}.get(profile, 2.0)
    scan_interval = max(1.0 / scan_fps, (end - start) / 7200)
    candidates = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            decoded = decode_frames(
                source_path, tmp, start=start, end=end, interval=scan_interval,
                max_frames=7201, max_dim=320, quality=60,
                timeout=settings.FFMPEG_TIMEOUT_SECONDS, check_cancel=ctx.check_cancel,
                heartbeat=ctx.heartbeat, keyframes_only=profile == "fast",
                hwaccel=_hwaccel(settings),
                source_codec=ctx.shared.get("video", {}).get("codec"),
                decode_stats=ctx.shared.setdefault("frame_decoding", {}),
            )
            previous = previous_edges = None
            for index, frame in enumerate(decoded):
                if index % 50 == 0:
                    ctx.check_cancel()
                image = cv2.imread(str(frame.path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                image = cv2.resize(image, (160, 90))
                edges = cv2.Canny(image, 80, 160)
                if previous is not None:
                    motion = float(np.abs(image.astype(np.float32) - previous).mean()) / 255
                    edge_change = float(np.abs(edges.astype(np.float32) - previous_edges).mean()) / 255
                    score = motion + 0.6 * edge_change
                    if score >= 0.025:
                        half_window = min(scan_interval / 4, 0.1)
                        candidates.append((max(start, frame.timestamp - half_window),
                                           min(end, frame.timestamp + half_window), score))
                previous = image.astype(np.float32)
                previous_edges = edges.astype(np.float32)
            ctx.shared["visual_scan"] = {
                "decoder": "ffmpeg", "profile": profile, "sampled_frames": len(decoded),
                "requested_interval_seconds": scan_interval, "max_dimension": 320,
                "keyframes_only": profile == "fast", "candidate_count": len(candidates),
                "signals": ["pixel_change", "edge_change"],
            }
    except FFmpegError as exc:
        ctx.warning(f"Visual prescan unavailable; using full-range coverage: {exc.message}")
        ctx.shared["visual_scan"] = {"decoder": "ffmpeg", "status": "fallback", "reason": exc.message}
    return candidates


def _select_adaptive_timestamps(
    duration: float, scenes: list[tuple[float, float, float]], target_frames: int, min_f: int, max_f: int
) -> list[Selection]:
    target = max(1, min(max_f, max(min_f, target_frames)))
    if duration <= 0:
        return [Selection(0.0, None)]
    # Keep half the budget distributed over the whole timeline. Rank visual
    # changes inside each coverage bin for the other half, then fill any
    # unused space with uniform samples. The actual requested budget wins.
    target = min(target, max(1, math.ceil(duration * 1000)))
    coverage_count = target if not scenes else max(1, (target + 1) // 2)
    chosen = {round(i * duration / coverage_count, 6): Selection(round(i * duration / coverage_count, 6), None)
              for i in range(coverage_count)}
    indexed = [(i, (a + b) / 2, score or 0.0) for i, (a, b, score) in enumerate(scenes)
               if 0 <= (a + b) / 2 < duration]
    bins: dict[int, list[tuple]] = {}
    for item in indexed:
        bins.setdefault(min(coverage_count - 1, int(item[1] / duration * coverage_count)), []).append(item)
    for candidates in bins.values():
        candidates.sort(key=lambda item: item[2], reverse=True)
    ranked = []
    while bins:
        for bucket in list(bins):
            ranked.append(bins[bucket].pop(0))
            if not bins[bucket]:
                del bins[bucket]
    for scene_id, timestamp, _ in ranked:
        if len(chosen) >= target:
            break
        timestamp = round(timestamp, 6)
        chosen.setdefault(timestamp, Selection(timestamp, scene_id))
    # A finer grid avoids collisions with the reserved coverage grid.
    for i in range(target * 2):
        if len(chosen) >= target:
            break
        timestamp = round(i * duration / (target * 2), 6)
        chosen.setdefault(timestamp, Selection(timestamp, None))
    return sorted(chosen.values(), key=lambda item: item.timestamp)


def _interval_for_count(duration: float, count: int) -> float:
    if count <= 0:
        return duration
    return max(duration / count, 0.1)


def resolve_interval(
    duration: float, requested_interval: float, cap: int | None
) -> dict:
    """Work out the interval that will actually be used, and say so.

    The old behaviour truncated from the FRONT of the video: asking for a
    frame every 0.2s on a 60-minute video produced 2000 frames covering only
    the first 6m40s, with no warning and a manifest that still claimed 0.2s
    sampling. Widening the interval instead keeps whole-video coverage, and
    the caller reports the change.
    """
    requested_interval = max(float(requested_interval or 0.0), 0.001)
    duration = max(float(duration or 0.0), 0.0)
    result = {
        "requested_interval_seconds": requested_interval,
        "effective_interval_seconds": requested_interval,
        "widened": False,
        "reason": None,
        "frame_count": 0,
    }
    if duration <= 0:
        result["frame_count"] = 1
        return result

    # The endpoint is exclusive: EOF is not a source frame. Avoid declaring
    # a 2,000-frame plan over budget just because t=duration was counted.
    wanted = max(1, math.ceil(duration / requested_interval - 1e-9))
    if cap is not None and wanted > cap:
        effective = duration / cap
        result["effective_interval_seconds"] = effective
        result["widened"] = True
        result["reason"] = (
            f"A frame every {requested_interval:g}s over {duration:.1f}s would be "
            f"{wanted} frames, above the MAX_FRAMES cap of {cap}. Widened to "
            f"{effective:.3f}s so the whole video is still covered."
        )
        result["frame_count"] = cap
    else:
        result["frame_count"] = wanted
    return result


def _select_interval_timestamps(
    duration: float, interval_seconds: float, cap: int | None
) -> list[Selection]:
    """Evenly spaced timestamps across the WHOLE video.

    Timestamps are computed as `i * interval` rather than by repeatedly adding,
    because float error compounds over thousands of iterations — at 0.2s over
    an hour the drift is visible in the filenames.
    """
    plan = resolve_interval(duration, interval_seconds, cap)
    interval = plan["effective_interval_seconds"]
    count = plan["frame_count"]
    if duration <= 0 or count <= 1:
        return [Selection(timestamp=0.0, scene_id=None)]
    timestamps = [round(i * interval, 6) for i in range(count) if i * interval < duration]
    if not timestamps:
        timestamps = [0.0]
    return [Selection(timestamp=ts, scene_id=None) for ts in timestamps]


def _segment_index_at(segments: list[dict], timestamp: float) -> int | None:
    """Index of the transcript segment spoken at this timestamp, if any."""
    for i, seg in enumerate(segments):
        start = seg.get("start")
        end = seg.get("end")
        if start is None or end is None:
            continue
        if start <= timestamp < end:
            return i
    return None


def _average_hash(image_path: str) -> int | None:
    """64-bit average hash for near-duplicate detection. Best-effort — returns
    None (treated as 'not comparable, keep the frame') on any failure."""
    try:
        import cv2

        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        small = cv2.resize(img, (8, 8))
        avg = float(small.mean())
        bits = 0
        for value in small.flatten():
            bits = (bits << 1) | (1 if value > avg else 0)
        return bits
    except Exception:  # noqa: BLE001 - hashing must never break extraction
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class ExtractFramesStep(PipelineStep):
    name = "extracting_frames"
    label = "Extracting frames"
    consumes = ("video",)
    produces = ("frames",)

    def run(self, ctx: PipelineContext) -> None:
        # Reuse exact timestamp requests across main/opening/event groups.
        # Paths, not JPEG byte strings, keep memory bounded at high budgets.
        with tempfile.TemporaryDirectory(prefix="frame-decode-") as cache_dir:
            self._cache_dir = Path(cache_dir)
            self._decoded_cache: dict[float, DecodedFrame] = {}
            self._last_timestamp: float | None = None
            self._run(ctx)

    def _run(self, ctx: PipelineContext) -> None:
        settings = get_settings()
        ctx.set_step_progress(self.name, 0)
        video_meta = ctx.shared["video"]
        duration = video_meta.get("duration_seconds") or 0.0
        fps = video_meta.get("fps") or 30.0
        mode = ctx.options.get("mode", "adaptive")
        source_path = str(ctx.storage.get(ctx.shared["source_relative_path"]))

        frame_format = ctx.options.get("frame_format", "jpeg")
        frame_max_dim = int(ctx.options.get("frame_max_dim", settings.FRAME_MAX_DIM_DEFAULT))
        quality = settings.FRAME_JPEG_QUALITY
        segments = (ctx.shared.get("transcript") or {}).get("segments") or []

        extract_kwargs = {
            "source_path": source_path,
            "duration": duration,
            "frame_format": frame_format,
            "frame_max_dim": frame_max_dim,
            "quality": quality,
            "settings": settings,
        }

        selections = self._select(mode, duration, fps, ctx, settings)
        range_start, range_end = _resolve_range(ctx, duration)
        budget = _frame_budget(ctx, settings)
        ctx.shared["frame_range"] = {
            "start_seconds": range_start, "end_seconds": range_end,
            "timestamp_origin": "original_video", "frame_budget": budget,
        }
        if mode == "every_frame":
            try:
                actual_frames = decode_frames(
                    source_path, self._cache_dir / "every-frame", start=range_start, end=range_end,
                    max_frames=budget + 1, max_dim=frame_max_dim, quality=quality, fmt=frame_format,
                    timeout=settings.FFMPEG_TIMEOUT_SECONDS, check_cancel=ctx.check_cancel,
                    heartbeat=ctx.heartbeat, hwaccel=_hwaccel(settings),
                    source_codec=video_meta.get("codec"), decode_stats=ctx.shared.setdefault("frame_decoding", {}),
                )
            except FFmpegError as exc:
                raise PipelineFailedError("frame_extraction_failed", exc.message, exc.to_detail()) from exc
            if len(actual_frames) > budget:
                raise PipelineFailedError("too_many_frames", f"The selected range has more than {budget} source frames. Narrow the range or raise the frame budget.")
            selections = [Selection(frame.timestamp, None) for frame in actual_frames]
            self._decoded_cache.update({round(frame.timestamp, 6): frame for frame in actual_frames})
            ctx.shared["interval_config"]["sampling_semantics"] = "every_decoded_source_frame"
        else:
            selections = self._add_bursts(ctx, selections, range_start, range_end, budget)
            self._prime_cache(ctx, selections, extract_kwargs)

        # NB: the interval modes cap themselves inside resolve_interval (by
        # widening the interval, not by dropping the tail), and adaptive is
        # bounded by ADAPTIVE_MAX_FRAMES, so a post-hoc truncation here would
        # be unreachable. A defensive clamp remains for any future mode.
        if len(selections) > budget:
            ctx.warning(
                f"{mode} mode produced {len(selections)} selections; "
                f"reducing evenly to the frame budget of {budget}"
            )
            selections = self._evenly_take(selections, budget)

        ctx.info(f"Selected {len(selections)} frame timestamps using mode={mode}")

        frames_meta: list[dict] = []
        failed_count = 0
        total = max(len(selections), 1)
        seen_source_timestamps: set[float] = set()
        seen_images: set[str] = set()
        stored_by_timestamp: dict[float, DecodedFrame] = {}
        last_progress = -1
        with tempfile.TemporaryDirectory() as tmp:
            for idx, sel in enumerate(selections):
                ctx.check_cancel()
                filename = frame_filename(sel.timestamp, frame_format)
                local_path = f"{tmp}/{filename}"
                extracted, last_error = self._extract_with_retries(ctx, sel.timestamp, local_path, **extract_kwargs)

                if not extracted:
                    failed_count += 1
                    ctx.warning(
                        f"Skipping frame at t={sel.timestamp:.3f}s after retries failed: "
                        f"{last_error.message if last_error else 'unknown error'}"
                    )
                    Path(local_path).unlink(missing_ok=True)
                    ctx.set_step_progress(self.name, round(5 + 65 * (idx + 1) / total))
                    continue

                actual_timestamp = self._last_timestamp if self._last_timestamp is not None else sel.timestamp
                # Sampling above the native FPS must not create duplicate
                # evidence or pretend interpolation supplied new information.
                actual_key = round(actual_timestamp, 6)
                if actual_key in seen_source_timestamps:
                    Path(local_path).unlink(missing_ok=True)
                    continue
                seen_source_timestamps.add(actual_key)
                filename = frame_filename(actual_timestamp, frame_format)
                if filename in seen_images:
                    stem, suffix = filename.rsplit(".", 1)
                    filename = f"{stem}-{len(frames_meta):06d}.{suffix}"
                seen_images.add(filename)

                with open(local_path, "rb") as f:
                    data = f.read()
                ctx.storage.save_bytes(ctx.job_relative("frames", filename), data)
                stored_by_timestamp[actual_key] = DecodedFrame(
                    ctx.storage.get(ctx.job_relative("frames", filename)), actual_timestamp,
                )
                Path(local_path).unlink(missing_ok=True)

                entry = {
                    "frame": len(frames_meta),
                    "timestamp": round(actual_timestamp, 6),
                    "requested_timestamp": round(sel.timestamp, 6),
                    "timestamp_source": "decoded_pts" if self._last_timestamp is not None else "requested",
                    "image": filename,
                    "mode": mode,
                    "category": "adaptive",
                    "extraction_reason": f"{mode} frame selection",
                    "transcript_segment_index": _segment_index_at(segments, actual_timestamp),
                }
                if mode == "adaptive":
                    entry["scene_id"] = sel.scene_id
                frames_meta.append(entry)

                progress = round(5 + 65 * (idx + 1) / total)
                if progress != last_progress:
                    ctx.set_step_progress(self.name, progress)
                    last_progress = progress

        if not frames_meta:
            raise PipelineFailedError(
                "frame_extraction_failed",
                "Every selected frame failed to extract; no usable frames were produced.",
            )
        if failed_count:
            ctx.warning(
                f"{failed_count} of {len(selections)} selected frames could not be extracted "
                f"and were skipped; {len(frames_meta)} frames were produced."
            )
        adaptive_count = len(frames_meta)
        # Saved artifacts now provide cross-group reuse; release obsolete
        # decode-cache copies before OCR/storyboards/ZIP need their disk space.
        previous_paths = {frame.path for frame in self._decoded_cache.values()}
        for requested, cached in self._decoded_cache.items():
            saved = stored_by_timestamp.get(round(cached.timestamp, 6))
            if saved is not None:
                self._decoded_cache[requested] = saved
        retained_paths = {frame.path for frame in self._decoded_cache.values()}
        cache_root = self._cache_dir.resolve()
        for path in previous_paths - retained_paths:
            if path.resolve().is_relative_to(cache_root):
                path.unlink(missing_ok=True)
        ctx.shared["frame_range"]["actual_frame_count"] = adaptive_count
        ctx.shared["frame_range"]["actual_average_fps"] = round(adaptive_count / max(range_end - range_start, 0.001), 6)
        if ctx.shared.get("adaptive_config") is not None:
            ctx.shared["adaptive_config"]["actual_frame_count"] = adaptive_count
        for burst in (ctx.shared.get("burst_config") or {}).get("windows", []):
            count = sum(burst["start_seconds"] <= frame["timestamp"] < burst["end_seconds"] for frame in frames_meta)
            burst["actual_frame_count"] = count
            burst["actual_average_fps"] = round(count / (burst["end_seconds"] - burst["start_seconds"]), 6)
        plan = ctx.shared.get("interval_config")
        if plan is not None:
            plan["actual_frame_count"] = adaptive_count
            plan["actual_average_fps"] = round(adaptive_count / max(range_end - range_start, 0.001), 6)
            plan.setdefault("sampling_semantics", "unique_source_frames_at_or_after_requested_times")

        # --- Dataset v2 frame groups (additive; never fail the job) ---
        dense_entries = self._extract_opening_dense(ctx, segments, extract_kwargs, start_index=len(frames_meta))
        frames_meta.extend(dense_entries)
        ctx.set_step_progress(self.name, 85)

        key_event_entries = self._extract_key_events(ctx, segments, extract_kwargs, start_index=len(frames_meta))
        frames_meta.extend(key_event_entries)

        ctx.shared["frames"] = frames_meta
        # frame_count stays "adaptive frames" for backward compatibility;
        # per-category counts land in the v2 manifest.
        ctx.shared["frame_count"] = adaptive_count
        ctx.shared["frame_counts_by_category"] = {
            "adaptive": adaptive_count,
            "opening_dense": len(dense_entries),
            "key_events": len(key_event_entries),
        }
        ctx.update_job({"frame_count": adaptive_count})
        ctx.set_step_progress(self.name, 100)

    @staticmethod
    def _evenly_take(items: list, count: int) -> list:
        if len(items) <= count:
            return items
        if count <= 1:
            return items[:count]
        return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]

    def _add_bursts(self, ctx: PipelineContext, base: list[Selection], start: float, end: float, budget: int) -> list[Selection]:
        requested = ctx.options.get("frame_bursts") or []
        if not requested:
            return base
        timestamps: set[float] = set()
        plans = []
        # Bound each temporary burst plan independently; even a malformed
        # request cannot allocate millions of Python objects before capping.
        for burst in requested[:8]:
            a = max(start, float(burst["start_seconds"]))
            b = min(end, float(burst["end_seconds"]))
            fps = min(60.0, max(1.0, float(burst["fps"])))
            if b <= a:
                continue
            selections = _select_interval_timestamps(b - a, 1.0 / fps, budget)
            stamps = [round(a + sel.timestamp, 6) for sel in selections]
            timestamps.update(stamps)
            plans.append({"start_seconds": a, "end_seconds": b, "requested_fps": fps,
                          "requested_frame_count": math.ceil((b - a) * fps - 1e-9)})
        if not timestamps:
            ctx.shared["burst_config"] = {"windows": [], "reason": "No burst overlaps the selected frame range."}
            return base
        # Retain at least half the total capacity for timeline coverage when
        # bursts alone would consume it. Unused coverage capacity is available
        # to the bursts, and overlapping timestamps cost only one slot.
        reserve = min(len(timestamps), budget // 2)
        base = self._evenly_take(base, max(1, budget - reserve))
        merged = {round(sel.timestamp, 6): sel for sel in base}
        extras = [ts for ts in sorted(timestamps) if ts not in merged]
        kept = self._evenly_take(extras, max(0, budget - len(merged)))
        merged.update({ts: Selection(ts, None) for ts in kept})
        for plan in plans:
            count = sum(plan["start_seconds"] <= ts < plan["end_seconds"] for ts in merged)
            plan["selected_frame_count"] = count
            plan["selected_average_fps"] = round(count / (plan["end_seconds"] - plan["start_seconds"]), 6)
        reduced = len(kept) < len(extras) or any(plan["selected_frame_count"] < plan["requested_frame_count"] for plan in plans)
        ctx.shared["burst_config"] = {"windows": plans, "budget_limited": reduced,
                                      "note": "Burst and coverage samples share the main frame budget; native video FPS also limits unique frames."}
        if reduced:
            ctx.warning("Burst sampling was reduced evenly to preserve timeline coverage within the frame budget.")
        if ctx.shared.get("interval_config") is not None:
            ctx.shared["interval_config"]["sampling_semantics"] = "coverage_plus_bursts"
            ctx.shared["interval_config"]["coverage_frame_count"] = len(base)
            ctx.shared["interval_config"]["coverage_average_interval_seconds"] = (end - start) / max(1, len(base))
        return sorted(merged.values(), key=lambda sel: sel.timestamp)

    def _prime_cache(self, ctx: PipelineContext, selections: list[Selection], kwargs: dict) -> None:
        """Decode dense neighborhoods once; keep sparse far-apart seeks cheap.

At most 256 targets enter one filter. Failed batches fall back to the
existing per-frame retries, without swallowing cancellation.
        """
        if not hasattr(self, "_cache_dir"):
            return
        missing = sorted({round(sel.timestamp, 6) for sel in selections if round(sel.timestamp, 6) not in self._decoded_cache})
        groups: list[list[float]] = []
        for timestamp in missing:
            if not groups or timestamp - groups[-1][-1] > 3.0 or len(groups[-1]) >= 256:
                groups.append([])
            groups[-1].append(timestamp)
        duration = kwargs["duration"]
        _, range_end = _resolve_range(ctx, duration)
        for group_index, targets in enumerate(groups):
            if len(targets) < 8:
                continue
            ctx.check_cancel()
            batch_dir = self._cache_dir / f"batch-{len(self._decoded_cache)}-{group_index}"
            try:
                frames = decode_frames(
                    kwargs["source_path"], batch_dir, start=targets[0],
                    end=min(range_end, targets[-1] + 2.0), timestamps=targets,
                    max_frames=len(targets), max_dim=kwargs["frame_max_dim"], quality=kwargs["quality"],
                    fmt=kwargs["frame_format"], timeout=kwargs["settings"].FFMPEG_TIMEOUT_SECONDS,
                    check_cancel=ctx.check_cancel, heartbeat=ctx.heartbeat, hwaccel=_hwaccel(kwargs["settings"]),
                    source_codec=ctx.shared.get("video", {}).get("codec"),
                    decode_stats=ctx.shared.setdefault("frame_decoding", {}),
                )
            except FFmpegError as exc:
                ctx.warning(f"Batch frame decoding failed; retrying individual timestamps: {exc.message}")
                continue
            times = [frame.timestamp for frame in frames]
            for target in targets:
                index = bisect.bisect_left(times, target - 1e-7)
                if index < len(frames):
                    self._decoded_cache[target] = frames[index]
            ctx.heartbeat()

    def _extract_with_retries(
        self,
        ctx: PipelineContext,
        timestamp: float,
        local_path: str,
        *,
        source_path: str,
        duration: float,
        frame_format: str,
        frame_max_dim: int,
        quality: int,
        settings,
    ) -> tuple[bool, "FFmpegError | None"]:
        # Clamp to just before the end — the exact reported duration can be a
        # hair past the last decodable frame.
        self._last_timestamp = None
        cached = getattr(self, "_decoded_cache", {}).get(round(timestamp, 6))
        if cached is not None and cached.path.exists():
            shutil.copyfile(cached.path, local_path)
            self._last_timestamp = cached.timestamp
            return True, None
        safe_ts = min(timestamp, max(duration - 0.000001, 0.0)) if duration else timestamp
        range_start, range_end = _resolve_range(ctx, duration)
        last_error: FFmpegError | None = None
        for attempt_ts, accurate in (
            (safe_ts, False),
            (safe_ts, True),
            (max(safe_ts - 0.25, range_start), True),
        ):
            try:
                ctx.check_cancel()
                actual = extract_frame_at(
                    source_path,
                    local_path,
                    attempt_ts,
                    max_dim=frame_max_dim,
                    quality=quality,
                    fmt=frame_format,
                    timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                    accurate=accurate,
                    check_cancel=ctx.check_cancel,
                    heartbeat=ctx.heartbeat,
                    hwaccel=_hwaccel(settings), source_codec=ctx.shared.get("video", {}).get("codec"),
                    decode_stats=ctx.shared.setdefault("frame_decoding", {}),
                )
                actual = float(actual) if isinstance(actual, (float, int)) else None
                if actual is not None and duration and not range_start <= actual < range_end:
                    raise FFmpegError("Decoded frame is outside the selected range", cmd=[], returncode=0, stderr="")
                self._last_timestamp = actual
                if hasattr(self, "_cache_dir"):
                    cached_path = self._cache_dir / f"cached-{len(self._decoded_cache):08d}.{frame_format}"
                    shutil.copyfile(local_path, cached_path)
                    self._decoded_cache[round(timestamp, 6)] = DecodedFrame(cached_path, actual if actual is not None else attempt_ts)
                return True, None
            except FFmpegError as exc:
                last_error = exc
        return False, last_error

    def _extract_opening_dense(
        self, ctx: PipelineContext, segments: list[dict], extract_kwargs: dict, *, start_index: int
    ) -> list[dict]:
        """Dense sampling of the opening seconds — where hooks live and early
        retention is won or lost."""
        settings = extract_kwargs["settings"]
        if not ctx.options.get("opening_dense_enabled", True):
            ctx.info("Opening-dense frames disabled for this job")
            ctx.shared["opening_dense_config"] = {
                "enabled": False,
                "duration_seconds": None,
                "interval_seconds": None,
                "source": "user_interface",
            }
            return []
        duration = extract_kwargs["duration"]
        window = float(ctx.options.get("opening_dense_duration") or settings.OPENING_DENSE_DURATION)
        interval = float(ctx.options.get("opening_dense_interval") or settings.OPENING_DENSE_INTERVAL)
        interval = max(interval, 0.05)
        window = min(window, duration) if duration else window

        # Record the *effective* config so the manifest can show what
        # actually ran, not the raw (possibly-null) request. Source label
        # tells the reader where each value came from. This must happen
        # before any early return below — a zero-length window still ran
        # with these effective values.
        user_duration = ctx.options.get("opening_dense_duration")
        user_interval = ctx.options.get("opening_dense_interval")
        config_source = "user_interface" if (user_duration is not None or user_interval is not None) else "default_configuration"
        ctx.shared["opening_dense_config"] = {
            "enabled": True,
            "duration_seconds": window,
            "interval_seconds": interval,
            "source": config_source,
        }

        timestamps = []
        range_start, range_end = _resolve_range(ctx, duration)
        t = math.ceil(range_start / interval - 1e-9) * interval
        while t < min(window, range_end) and len(timestamps) < 200:
            timestamps.append(round(t, 3))
            t += interval
        if not timestamps:
            return []

        ctx.info(
            f"Extracting {len(timestamps)} opening-dense frames "
            f"(first {window:.2f}s at {interval:.2f}s intervals)"
        )
        entries: list[dict] = []
        frame_format = extract_kwargs["frame_format"]
        self._prime_cache(ctx, [Selection(ts, None) for ts in timestamps], extract_kwargs)
        seen_actual: set[float] = set()
        with tempfile.TemporaryDirectory() as tmp:
            for ts in timestamps:
                ctx.check_cancel()
                filename = frame_filename(ts, frame_format)
                local_path = f"{tmp}/{filename}"
                extracted, last_error = self._extract_with_retries(ctx, ts, local_path, **extract_kwargs)
                if not extracted:
                    ctx.warning(
                        f"Skipping opening-dense frame at t={ts:.3f}s: "
                        f"{last_error.message if last_error else 'unknown error'}"
                    )
                    continue
                actual_ts = self._last_timestamp if self._last_timestamp is not None else ts
                if round(actual_ts, 6) in seen_actual:
                    continue
                seen_actual.add(round(actual_ts, 6))
                filename = frame_filename(actual_ts, frame_format)
                with open(local_path, "rb") as f:
                    data = f.read()
                ctx.storage.save_bytes(ctx.job_relative("frames", "opening_dense", filename), data)
                entries.append(
                    {
                        "frame": start_index + len(entries),
                        "timestamp": round(actual_ts, 6),
                        "requested_timestamp": ts,
                        "timestamp_source": "decoded_pts" if self._last_timestamp is not None else "requested",
                        "image": f"opening_dense/{filename}",
                        "mode": "dense_interval",  # R1.5: was inheriting the job's mode, which was contradictory
                        "category": "opening_dense",
                        "extraction_reason": f"Dense sampling of the first {window:.2f}s (every {interval:.2f}s)",
                        "transcript_segment_index": _segment_index_at(segments, actual_ts),
                    }
                )
        return entries

    def _extract_key_events(
        self, ctx: PipelineContext, segments: list[dict], extract_kwargs: dict, *, start_index: int
    ) -> list[dict]:
        """Frames just before / at / just after each user-declared event, with
        perceptual-hash suppression of near-identical frames."""
        events = [e for e in (ctx.options.get("events") or []) if isinstance(e.get("time_seconds"), (int, float))]
        # Record what actually ran either way, so the manifest can't drift.
        ctx.shared["key_events_config"] = {
            "enabled": bool(events),
            "offsets_seconds": [-0.25, 0.0, 0.25],
            "event_count": len(events),
        }
        if not events:
            return []

        duration = extract_kwargs["duration"]
        range_start, range_end = _resolve_range(ctx, duration)
        frame_format = extract_kwargs["frame_format"]
        ctx.info(f"Extracting key-event frames for {len(events)} event(s)")
        entries: list[dict] = []
        kept_hashes: list[int] = []
        seen_filenames: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            for event in events:
                event_time = float(event["time_seconds"])
                event_id = event.get("id") or f"event_{event_time:.2f}"
                label = event.get("label") or event.get("type") or "event"
                for offset in (-0.25, 0.0, 0.25):
                    ctx.check_cancel()
                    ts = max(0.0, event_time + offset)
                    if duration:
                        ts = min(ts, max(duration - 0.000001, 0.0))
                    if duration and not range_start <= ts < range_end:
                        continue
                    filename = frame_filename(ts, frame_format)
                    if filename in seen_filenames:
                        continue
                    local_path = f"{tmp}/{filename}"
                    extracted, last_error = self._extract_with_retries(ctx, ts, local_path, **extract_kwargs)
                    if not extracted:
                        ctx.warning(
                            f"Skipping key-event frame at t={ts:.3f}s ({label}): "
                            f"{last_error.message if last_error else 'unknown error'}"
                        )
                        continue

                    actual_ts = self._last_timestamp if self._last_timestamp is not None else ts
                    filename = frame_filename(actual_ts, frame_format)
                    if filename in seen_filenames:
                        continue

                    frame_hash = _average_hash(local_path)
                    if frame_hash is not None and any(_hamming(frame_hash, h) <= 2 for h in kept_hashes):
                        ctx.info(f"Skipping near-duplicate key-event frame at t={ts:.3f}s ({label})")
                        continue

                    with open(local_path, "rb") as f:
                        data = f.read()
                    ctx.storage.save_bytes(ctx.job_relative("frames", "key_events", filename), data)
                    seen_filenames.add(filename)
                    if frame_hash is not None:
                        kept_hashes.append(frame_hash)
                    entries.append(
                        {
                            "frame": start_index + len(entries),
                            "timestamp": round(actual_ts, 6),
                            "requested_timestamp": round(ts, 6),
                            "timestamp_source": "decoded_pts" if self._last_timestamp is not None else "requested",
                            "image": f"key_events/{filename}",
                            "mode": "key_event",  # R1.5: was inheriting the job's mode, which was contradictory
                            "category": "key_event",
                            "extraction_reason": f"Key event '{label}' ({offset:+.2f}s)",
                            "event_id": event_id,
                            "transcript_segment_index": _segment_index_at(segments, actual_ts),
                            "phash": format(frame_hash, "016x") if frame_hash is not None else None,
                        }
                    )
        return entries

    def _select(
        self, mode: str, duration: float, fps: float, ctx: PipelineContext, settings
    ) -> list[Selection]:
        start, end = _resolve_range(ctx, duration)
        span = end - start
        budget = _frame_budget(ctx, settings)
        if mode == "adaptive":
            target = int(ctx.options.get("target_frames", settings.DEFAULT_TARGET_FRAMES))
            target = min(budget, max(settings.ADAPTIVE_MIN_FRAMES, min(settings.ADAPTIVE_MAX_FRAMES, target)))
            source_path = str(ctx.storage.get(ctx.shared["source_relative_path"]))
            scenes = _detect_scenes(source_path, ctx)
            ctx.info(f"Visual prescan found {len(scenes)} change candidates")
            relative = [(max(start, a) - start, min(end, b) - start, score)
                        for a, b, score in scenes if a < end and b > start]
            # Spoken instructions and objective-matching transcript lines
            # supply additional timing hints; they are evidence for sampling,
            # not visual descriptions invented from the audio.
            objective_words = set(str(ctx.options.get("analysis_objective") or "").lower().split())
            for segment in (ctx.shared.get("transcript") or {}).get("segments") or []:
                timestamp = float(segment.get("start") or 0)
                if start <= timestamp < end:
                    relevant = bool(objective_words & set(str(segment.get("text") or "").lower().split()))
                    center = timestamp - start
                    relative.append((center, min(span, center + 0.1), 0.2 if relevant else 0.04))
            selections = _select_adaptive_timestamps(
                span, relative, target, min(settings.ADAPTIVE_MIN_FRAMES, budget), min(settings.ADAPTIVE_MAX_FRAMES, budget)
            )
            ctx.shared["adaptive_config"] = {"target_frames": target, "selected_frames": len(selections),
                                             "coverage_fraction": 0.5, "selection": "coverage_and_visual_transcript_changes"}
            return [Selection(round(start + sel.timestamp, 6), sel.scene_id) for sel in selections]

        if mode == "interval":
            interval_ms = int(ctx.options.get("interval_ms", 1000))
            if interval_ms < 17:
                raise PipelineFailedError(
                    "invalid_interval", "interval_ms must be at least 17ms"
                )
            return self._interval_selection(ctx, span, interval_ms / 1000.0, settings, start=start)

        if mode == "per_second":
            return self._interval_selection(ctx, span, 1.0, settings, start=start)

        if mode == "every_frame":
            estimated = int(span * fps)
            if start == 0 and end == duration and estimated > budget * 1.02:
                raise PipelineFailedError(
                    "too_many_frames",
                    f"every_frame mode would extract ~{estimated} frames, exceeding the "
                    f"cap of {budget}. Narrow the frame range, raise the budget, or use adaptive mode, which "
                    f"selects a small representative set automatically.",
                    {"estimated_frames": estimated, "max_frames": budget},
                )
            frame_interval = 1.0 / fps if fps else 1.0
            return self._interval_selection(ctx, span, frame_interval, settings, start=start)

        raise PipelineFailedError("invalid_mode", f"Unknown extraction mode: {mode}")

    @staticmethod
    def _interval_selection(
        ctx: PipelineContext, duration: float, interval: float, settings, *, start: float = 0.0
    ) -> list[Selection]:
        """Evenly spaced selection that records — and loudly reports — the
        interval actually used when the request exceeds MAX_FRAMES."""
        budget = _frame_budget(ctx, settings)
        plan = resolve_interval(duration, interval, budget)
        plan["range_start_seconds"] = start
        plan["range_end_seconds"] = start + duration
        ctx.shared["interval_config"] = plan
        if plan["widened"]:
            ctx.warning(plan["reason"])
        return [Selection(round(start + sel.timestamp, 6), sel.scene_id)
                for sel in _select_interval_timestamps(duration, interval, budget)]
