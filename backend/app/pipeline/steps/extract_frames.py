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

        selections = self._select(mode, duration, fps, ctx, settings)

        if len(selections) > settings.MAX_FRAMES:
            ctx.warning(
                f"{mode} mode would produce {len(selections)} frames; "
                f"truncating to MAX_FRAMES={settings.MAX_FRAMES}"
            )
            selections = selections[: settings.MAX_FRAMES]

        ctx.info(f"Selected {len(selections)} frame timestamps using mode={mode}")

        frames_meta = []
        failed_count = 0
        total = max(len(selections), 1)
        with tempfile.TemporaryDirectory() as tmp:
            for idx, sel in enumerate(selections):
                ctx.check_cancel()
                # Clamp to just before the end — the exact reported duration
                # can be a hair past the last decodable frame.
                safe_ts = min(sel.timestamp, max(duration - 0.05, 0.0)) if duration else sel.timestamp
                filename = frame_filename(sel.timestamp, frame_format)
                local_path = f"{tmp}/{filename}"

                last_error: FFmpegError | None = None
                extracted = False
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
                        extracted = True
                        break
                    except FFmpegError as exc:
                        last_error = exc

                if not extracted:
                    failed_count += 1
                    ctx.warning(
                        f"Skipping frame at t={sel.timestamp:.3f}s after retries failed: "
                        f"{last_error.message if last_error else 'unknown error'}"
                    )
                    ctx.set_step_progress(self.name, round(5 + 90 * (idx + 1) / total))
                    continue

                with open(local_path, "rb") as f:
                    data = f.read()
                ctx.storage.save_bytes(ctx.job_relative("frames", filename), data)

                entry = {
                    "frame": len(frames_meta),
                    "timestamp": round(sel.timestamp, 3),
                    "image": filename,
                    "mode": mode,
                }
                if mode == "adaptive":
                    entry["scene_id"] = sel.scene_id
                frames_meta.append(entry)

                ctx.set_step_progress(self.name, round(5 + 90 * (idx + 1) / total))

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

        ctx.shared["frames"] = frames_meta
        ctx.shared["frame_count"] = len(frames_meta)
        ctx.update_job({"frame_count": len(frames_meta)})
        ctx.set_step_progress(self.name, 100)

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
