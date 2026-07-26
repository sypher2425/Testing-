"""Frame extraction step.

adaptive mode (flagship): PySceneDetect content-aware scene detection targeting
a small, representative frame set (30-150 frames by default). interval/
per_second/every_frame exist for exhaustive extraction but every_frame is
hard-capped and rejected on videos that would exceed it.
"""
import tempfile
from dataclasses import dataclass

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.ffmpeg import FFmpegError, extract_frame_at
from app.utils.filenames import frame_filename


@dataclass
class Selection:
    timestamp: float
    scene_id: int | None


def _detect_scenes(source_path: str, ctx: PipelineContext) -> list[tuple[float, float, float]]:
    """Returns list of (start_sec, end_sec, score) per detected scene."""
    from scenedetect import SceneManager, StatsManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(source_path)
    stats_manager = StatsManager()
    scene_manager = SceneManager(stats_manager)
    scene_manager.add_detector(ContentDetector())
    scene_manager.detect_scenes(video, show_progress=False)
    scene_list = scene_manager.get_scene_list()

    scenes = []
    for start_tc, end_tc in scene_list:
        start_sec = start_tc.get_seconds()
        end_sec = end_tc.get_seconds()
        score = None
        try:
            frame_num = start_tc.get_frames()
            score = stats_manager.get_metrics(frame_num, [ContentDetector.FRAME_SCORE_KEY])[0]
        except Exception:  # noqa: BLE001
            score = None
        scenes.append((start_sec, end_sec, score))

    if scenes and any(s[2] is None for s in scenes):
        # Fallback heuristic when the stats API doesn't expose a score in this
        # version: shorter scenes generally indicate faster-changing content.
        durations = [end - start for start, end, _ in scenes]
        mean_dur = sum(durations) / len(durations) if durations else 1.0
        scenes = [
            (start, end, mean_dur / max(end - start, 0.01)) for start, end, _ in scenes
        ]
    return scenes


def _select_adaptive_timestamps(
    duration: float, scenes: list[tuple[float, float, float]], target_frames: int, min_f: int, max_f: int
) -> list[Selection]:
    if not scenes:
        return _select_interval_timestamps(duration, _interval_for_count(duration, target_frames), None)

    indexed = [
        (i, (start + end) / 2.0, score if score is not None else 0.0)
        for i, (start, end, score) in enumerate(scenes)
    ]

    if len(indexed) < min_f:
        # Fall back to interval sampling to reach the minimum, merged with scene midpoints.
        needed = min_f - len(indexed)
        interval = duration / (needed + 1) if needed > 0 else duration
        existing_ts = {round(mid, 1) for _, mid, _ in indexed}
        extra = []
        t = interval
        while len(extra) < needed and t < duration:
            if round(t, 1) not in existing_ts:
                extra.append(t)
            t += interval
        combined = [(mid, score) for _, mid, score in indexed] + [(t, 0.0) for t in extra]
        combined.sort(key=lambda x: x[0])
        return [Selection(timestamp=ts, scene_id=i) for i, (ts, _) in enumerate(combined)]

    if len(indexed) > max_f:
        # Keep the highest-content-change scenes, then restore chronological order.
        ranked = sorted(indexed, key=lambda x: x[2], reverse=True)[:max_f]
        ranked.sort(key=lambda x: x[1])
        return [Selection(timestamp=mid, scene_id=orig_i) for orig_i, mid, _ in ranked]

    return [Selection(timestamp=mid, scene_id=orig_i) for orig_i, mid, _ in indexed]


def _interval_for_count(duration: float, count: int) -> float:
    if count <= 0:
        return duration
    return max(duration / count, 0.1)


def _select_interval_timestamps(
    duration: float, interval_seconds: float, cap: int | None
) -> list[Selection]:
    timestamps = []
    t = 0.0
    while t < duration:
        timestamps.append(t)
        t += interval_seconds
        if cap is not None and len(timestamps) >= cap:
            break
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

        if len(selections) > settings.MAX_FRAMES:
            ctx.warning(
                f"{mode} mode would produce {len(selections)} frames; "
                f"truncating to MAX_FRAMES={settings.MAX_FRAMES}"
            )
            selections = selections[: settings.MAX_FRAMES]

        ctx.info(f"Selected {len(selections)} frame timestamps using mode={mode}")

        frames_meta: list[dict] = []
        failed_count = 0
        total = max(len(selections), 1)
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
                    ctx.set_step_progress(self.name, round(5 + 65 * (idx + 1) / total))
                    continue

                with open(local_path, "rb") as f:
                    data = f.read()
                ctx.storage.save_bytes(ctx.job_relative("frames", filename), data)

                entry = {
                    "frame": len(frames_meta),
                    "timestamp": round(sel.timestamp, 3),
                    "image": filename,
                    "mode": mode,
                    "category": "adaptive",
                    "extraction_reason": f"{mode} frame selection",
                    "transcript_segment_index": _segment_index_at(segments, sel.timestamp),
                }
                if mode == "adaptive":
                    entry["scene_id"] = sel.scene_id
                frames_meta.append(entry)

                ctx.set_step_progress(self.name, round(5 + 65 * (idx + 1) / total))

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
        safe_ts = min(timestamp, max(duration - 0.05, 0.0)) if duration else timestamp
        last_error: FFmpegError | None = None
        for attempt_ts, accurate in (
            (safe_ts, False),
            (safe_ts, True),
            (max(safe_ts - 0.25, 0.0), True),
        ):
            try:
                extract_frame_at(
                    source_path,
                    local_path,
                    attempt_ts,
                    max_dim=frame_max_dim,
                    quality=quality,
                    fmt=frame_format,
                    timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                    accurate=accurate,
                )
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
        t = 0.0
        while t < window and len(timestamps) < 200:
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
                with open(local_path, "rb") as f:
                    data = f.read()
                ctx.storage.save_bytes(ctx.job_relative("frames", "opening_dense", filename), data)
                entries.append(
                    {
                        "frame": start_index + len(entries),
                        "timestamp": ts,
                        "image": f"opening_dense/{filename}",
                        "mode": "dense_interval",  # R1.5: was inheriting the job's mode, which was contradictory
                        "category": "opening_dense",
                        "extraction_reason": f"Dense sampling of the first {window:.2f}s (every {interval:.2f}s)",
                        "transcript_segment_index": _segment_index_at(segments, ts),
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
                        ts = min(ts, max(duration - 0.05, 0.0))
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
                            "timestamp": round(ts, 3),
                            "image": f"key_events/{filename}",
                            "mode": "key_event",  # R1.5: was inheriting the job's mode, which was contradictory
                            "category": "key_event",
                            "extraction_reason": f"Key event '{label}' ({offset:+.2f}s)",
                            "event_id": event_id,
                            "transcript_segment_index": _segment_index_at(segments, ts),
                            "phash": format(frame_hash, "016x") if frame_hash is not None else None,
                        }
                    )
        return entries

    def _select(
        self, mode: str, duration: float, fps: float, ctx: PipelineContext, settings
    ) -> list[Selection]:
        if mode == "adaptive":
            target = int(ctx.options.get("target_frames", settings.DEFAULT_TARGET_FRAMES))
            target = max(settings.ADAPTIVE_MIN_FRAMES, min(settings.ADAPTIVE_MAX_FRAMES, target))
            source_path = str(ctx.storage.get(ctx.shared["source_relative_path"]))
            scenes = _detect_scenes(source_path, ctx)
            ctx.info(f"PySceneDetect found {len(scenes)} scenes")
            return _select_adaptive_timestamps(
                duration, scenes, target, settings.ADAPTIVE_MIN_FRAMES, settings.ADAPTIVE_MAX_FRAMES
            )

        if mode == "interval":
            interval_ms = int(ctx.options.get("interval_ms", 1000))
            if interval_ms < 100:
                raise PipelineFailedError(
                    "invalid_interval", "interval_ms must be at least 100ms"
                )
            return _select_interval_timestamps(duration, interval_ms / 1000.0, settings.MAX_FRAMES)

        if mode == "per_second":
            return _select_interval_timestamps(duration, 1.0, settings.MAX_FRAMES)

        if mode == "every_frame":
            estimated = int(duration * fps)
            if estimated > settings.MAX_FRAMES:
                raise PipelineFailedError(
                    "too_many_frames",
                    f"every_frame mode would extract ~{estimated} frames, exceeding the "
                    f"cap of {settings.MAX_FRAMES}. Use adaptive mode instead, which "
                    f"selects a small representative set automatically.",
                    {"estimated_frames": estimated, "max_frames": settings.MAX_FRAMES},
                )
            frame_interval = 1.0 / fps if fps else 1.0
            return _select_interval_timestamps(duration, frame_interval, settings.MAX_FRAMES)

        raise PipelineFailedError("invalid_mode", f"Unknown extraction mode: {mode}")
