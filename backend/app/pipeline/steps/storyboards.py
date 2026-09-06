"""Builds storyboard contact sheets from the already-extracted frames.

The dataset's individual frames are complete but hard to reason about: an AI
asked about pacing or prompt-to-result timing has to open hundreds of files and
reconstruct the timeline itself. This step composes them into labeled grids —
one tile per frame, chronological left-to-right then top-to-bottom, each
carrying its frame number, timestamp and (when available) the transcript line
spoken at that moment.

Five views, because they answer different questions:

  adaptive     the representative pass over the whole video
  opening_dense  the first seconds in detail (hook, first interaction)
  timeline     an even sample, independent of the adaptive algorithm
  transcript   one frame per spoken line — does the visual match the words?
  key_moments  a short summary of the biggest visual changes

This step is additive and must never fail a job whose frames and transcript are
already on disk: every failure path records a status in the manifest instead of
raising.
"""
import json
import logging
from pathlib import Path

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.utils.storyboard import (
    TileSpec,
    chunk_tiles,
    compose_sheet,
    format_timestamp,
    plan_sheets,
)
from app.utils.timeouts import HeartbeatTicker

logger = logging.getLogger("pipeline.storyboards")

STORYBOARD_DIR = "storyboards"
STORYBOARD_MANIFEST = "storyboard_manifest.json"

# How a frame's category maps onto the sheet families that use it directly.
_CATEGORY_ADAPTIVE = "adaptive"
_CATEGORY_OPENING = "opening_dense"


def _option(options: dict, key: str, default):
    """Read a job option, treating an explicit None as 'not set'.

    Nullable fields on CreateJobOptions (storyboard_columns,
    storyboard_tiles_per_sheet) are persisted into Job.options with a literal
    None, so `options.get(key, default)` returns that None rather than the
    default — the key is present. That is how `int(None)` reached production.

    `default if value is None` rather than `value or default` on purpose:
    False is a real value for the boolean toggles and must survive.
    """
    value = options.get(key)
    return default if value is None else value


def _sorted_by_time(frames: list[dict]) -> list[dict]:
    """ctx.shared['frames'] is adaptive + opening_dense + key_events
    concatenated, so it jumps backwards in time twice. Every sheet needs real
    chronological order."""
    return sorted(frames, key=lambda f: (f.get("timestamp") or 0.0, f.get("frame") or 0))


def _segment_at(segments: list[dict], timestamp: float) -> tuple[int | None, dict | None]:
    for i, seg in enumerate(segments):
        start, end = seg.get("start"), seg.get("end")
        if start is None or end is None:
            continue
        if start <= timestamp < end:
            return i, seg
    return None, None


def _frame_path(ctx: PipelineContext, frame: dict) -> Path | None:
    """Absolute path of a frame image. `image` is a bare filename for adaptive
    frames and a sub-path like 'opening_dense/0000.250.jpg' for the others."""
    image = frame.get("image")
    if not image:
        return None
    try:
        return Path(ctx.storage.get(ctx.job_relative("frames", *str(image).split("/"))))
    except Exception:  # noqa: BLE001 - a bad path must not kill the sheet
        return None


class StoryboardStep(PipelineStep):
    name = "generating_storyboards"
    label = "Building storyboards"
    consumes = ("frames",)
    produces = ("storyboards",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        settings = get_settings()

        if not _option(ctx.options, "storyboard_enabled", settings.STORYBOARD_ENABLED):
            ctx.info("Storyboards disabled for this job")
            ctx.shared["storyboards"] = {"status": "skipped", "reason": "disabled_by_option"}
            ctx.set_step_progress(self.name, 100)
            return

        frames = ctx.shared.get("frames") or []
        if not frames:
            ctx.warning("No frames were extracted; skipping storyboards")
            ctx.shared["storyboards"] = {"status": "skipped", "reason": "no_frames"}
            ctx.set_step_progress(self.name, 100)
            return

        try:
            with HeartbeatTicker(settings.HEARTBEAT_INTERVAL_SECONDS, ctx.heartbeat):
                result = self._build_all(ctx, settings, frames)
            ctx.shared["storyboards"] = result
            ctx.info(
                f"Storyboards: {len(result['storyboards'])} sheet(s) across "
                f"{len(result['types_built'])} view(s)"
            )
        except Exception as exc:  # noqa: BLE001 - additive; never fail the job
            logger.exception("Storyboard generation failed for job %s", ctx.job_id)
            ctx.error(
                f"Storyboard generation failed ({type(exc).__name__}: {exc}). The frames and "
                "transcript are unaffected — you can retry with the Regenerate button."
            )
            ctx.shared["storyboards"] = {
                "status": "extraction_failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "storyboards": [],
            }
        ctx.set_step_progress(self.name, 100)

    # ------------------------------------------------------------------ build

    def _build_all(self, ctx: PipelineContext, settings, frames: list[dict]) -> dict:
        video = ctx.shared.get("video") or {}
        transcript = ctx.shared.get("transcript") or {}
        segments = transcript.get("segments") or []

        options = ctx.options
        include_captions = bool(
            _option(options, "storyboard_include_captions", settings.STORYBOARD_INCLUDE_CAPTIONS)
        )
        columns_override = _option(options, "storyboard_columns", None)
        theme = str(_option(options, "storyboard_theme", settings.STORYBOARD_THEME))
        quality = int(_option(options, "storyboard_quality", settings.STORYBOARD_JPEG_QUALITY))
        max_tiles = int(
            _option(options, "storyboard_tiles_per_sheet", settings.STORYBOARD_MAX_TILES_PER_SHEET)
        )

        # Tile geometry follows the source frames, not the source video: the
        # frames are what actually get pasted.
        probe_w, probe_h = self._frame_dimensions(ctx, frames, video)
        plan = plan_sheets(
            frame_width=probe_w,
            frame_height=probe_h,
            columns=int(columns_override) if columns_override else None,
            sheet_width=int(settings.STORYBOARD_SHEET_WIDTH_PX),
            max_tiles_per_sheet=max_tiles,
            max_sheet_height=int(settings.STORYBOARD_MAX_SHEET_HEIGHT_PX),
            include_captions=include_captions,
        )

        groups = self._collect_groups(ctx, settings, frames, segments, plan.tiles_per_sheet)
        built: list[dict] = []
        types_built: list[str] = []
        placeholders_total = 0

        total_sheets = sum(
            len(chunk_tiles(tiles, plan.tiles_per_sheet)) for tiles in groups.values() if tiles
        )
        done = 0

        for board_type, tiles in groups.items():
            if not tiles:
                continue
            pages = chunk_tiles(tiles, plan.tiles_per_sheet)
            single = board_type == "key_moments" and len(pages) == 1
            for page_index, page in enumerate(pages, start=1):
                ctx.check_cancel()
                filename = (
                    "key_moments_storyboard.jpg"
                    if single
                    else f"{self._basename(board_type)}_{page_index:02d}.jpg"
                )
                span = f"{format_timestamp(page[0].timestamp_seconds)}–{format_timestamp(page[-1].timestamp_seconds)}"
                title = f"{filename}  ·  {len(page)} frames  ·  {span}"
                data, placeholders = compose_sheet(
                    page, plan, title=title, theme_name=theme, quality=quality
                )
                placeholders_total += placeholders
                ctx.storage.save_bytes(ctx.job_relative(STORYBOARD_DIR, filename), data)
                built.append(
                    {
                        "type": board_type,
                        "file": f"{STORYBOARD_DIR}/{filename}",
                        "sheet_index": page_index,
                        "sheet_count": len(pages),
                        "columns": plan.columns,
                        "rows": max(1, -(-len(page) // plan.columns)),
                        "tile_width": plan.tile_width,
                        "tile_height": plan.tile_height,
                        "size_bytes": len(data),
                        "unavailable_frames": placeholders,
                        "frames": [
                            self._tile_record(i, tile) for i, tile in enumerate(page, start=1)
                        ],
                    }
                )
                done += 1
                ctx.set_step_progress(self.name, min(95, round(95 * done / max(total_sheets, 1))))
            types_built.append(board_type)

        manifest = {
            "status": "success",
            "generated_at": None,  # filled by the caller that knows the clock
            "video": {
                "filename": ctx.shared.get("original_filename"),
                "durationSeconds": video.get("duration_seconds"),
                "width": video.get("width"),
                "height": video.get("height"),
                "fps": video.get("fps"),
            },
            "layout": {
                "sheet_width": plan.sheet_width,
                "columns": plan.columns,
                "rows_per_sheet": plan.rows,
                "tiles_per_sheet": plan.tiles_per_sheet,
                "tile_width": plan.tile_width,
                "tile_height": plan.tile_height,
                "captions": include_captions,
                "theme": theme,
                "jpeg_quality": quality,
            },
            "types_built": types_built,
            "unavailable_frames": placeholders_total,
            "storyboards": built,
        }

        from app.utils.timestamps import now_utc_iso

        manifest["generated_at"] = now_utc_iso()
        ctx.storage.save_bytes(
            ctx.job_relative(STORYBOARD_MANIFEST), json.dumps(manifest, indent=2).encode()
        )
        if placeholders_total:
            ctx.warning(
                f"{placeholders_total} storyboard tile(s) could not read their source frame "
                "and show a placeholder."
            )
        return manifest

    @staticmethod
    def _basename(board_type: str) -> str:
        return {
            "adaptive": "adaptive_storyboard",
            "opening_dense": "opening_dense_storyboard",
            "timeline": "timeline_storyboard",
            "transcript": "transcript_storyboard",
            "key_moments": "key_moments_storyboard",
        }.get(board_type, f"{board_type}_storyboard")

    @staticmethod
    def _tile_record(index: int, tile: TileSpec) -> dict:
        record = {
            "tileIndex": index,
            "frameNumber": tile.frame_number,
            "timestampSeconds": round(tile.timestamp_seconds, 3),
            "timestampLabel": format_timestamp(tile.timestamp_seconds),
            "sourceFrame": tile.source_frame_rel,
            "transcriptSegment": tile.extra.get("transcript_segment"),
        }
        record.update({k: v for k, v in tile.extra.items() if k != "transcript_segment"})
        return record

    def _frame_dimensions(self, ctx: PipelineContext, frames: list[dict], video: dict) -> tuple[int, int]:
        """Read the real dimensions of an extracted frame, falling back to the
        video's. The frames are downscaled copies, so their aspect ratio is what
        the tiles must match."""
        for frame in frames[:5]:
            path = _frame_path(ctx, frame)
            if path and path.is_file():
                try:
                    from PIL import Image

                    with Image.open(path) as img:
                        return img.width, img.height
                except Exception:  # noqa: BLE001
                    continue
        return int(video.get("width") or 1080), int(video.get("height") or 1920)

    # ----------------------------------------------------------- tile groups

    def _collect_groups(
        self,
        ctx: PipelineContext,
        settings,
        frames: list[dict],
        segments: list[dict],
        sheet_capacity: int,
    ) -> dict[str, list[TileSpec]]:
        chronological = _sorted_by_time(frames)
        adaptive = [f for f in chronological if (f.get("category") or "adaptive") == _CATEGORY_ADAPTIVE]
        opening = [f for f in chronological if f.get("category") == _CATEGORY_OPENING]

        return {
            "adaptive": [self._tile(ctx, f, segments) for f in adaptive],
            "opening_dense": [self._tile(ctx, f, segments) for f in opening],
            "timeline": self._timeline_tiles(ctx, settings, chronological, segments),
            "transcript": self._transcript_tiles(ctx, chronological, segments),
            "key_moments": self._key_moment_tiles(
                ctx, settings, chronological, segments, sheet_capacity
            ),
        }

    def _tile(
        self, ctx: PipelineContext, frame: dict, segments: list[dict], *, subtitle: str | None = None
    ) -> TileSpec:
        timestamp = float(frame.get("timestamp") or 0.0)
        seg_index, seg = _segment_at(segments, timestamp)
        if seg_index is None:
            seg_index = frame.get("transcript_segment_index")
            if isinstance(seg_index, int) and 0 <= seg_index < len(segments):
                seg = segments[seg_index]

        caption = (seg or {}).get("text") if seg else None
        extra: dict = {}
        if seg is not None and seg_index is not None:
            extra["transcript_segment"] = {
                "index": seg_index,
                "start": seg.get("start"),
                "end": seg.get("end"),
                # The manifest keeps the COMPLETE text even when the tile
                # caption had to be wrapped or ellipsised.
                "text": seg.get("text"),
            }
        if frame.get("scene_id") is not None:
            extra["sceneId"] = frame["scene_id"]
        if frame.get("category"):
            extra["category"] = frame["category"]
        if frame.get("event_id"):
            extra["eventId"] = frame["event_id"]

        if subtitle is None and frame.get("scene_id") is not None:
            subtitle = f"SCENE {frame['scene_id']}"

        return TileSpec(
            source_path=_frame_path(ctx, frame),
            frame_number=int(frame.get("frame") or 0),
            timestamp_seconds=timestamp,
            caption=caption,
            subtitle=subtitle,
            source_frame_rel=f"frames/{frame.get('image')}",
            extra=extra,
        )

    def _timeline_tiles(
        self, ctx: PipelineContext, settings, chronological: list[dict], segments: list[dict]
    ) -> list[TileSpec]:
        """Evenly spaced overview, independent of the adaptive algorithm.

        Samples the frames that already exist rather than re-decoding the video.
        The interval widens automatically when the extracted frames are sparser
        than the requested tick, so the sheet never repeats the same frame.
        """
        if not chronological:
            return []
        duration = float((ctx.shared.get("video") or {}).get("duration_seconds") or 0.0)
        if duration <= 0:
            duration = float(chronological[-1].get("timestamp") or 0.0) or 1.0

        interval = float(
            _option(
                ctx.options,
                "storyboard_timeline_interval",
                settings.STORYBOARD_TIMELINE_INTERVAL_SECONDS,
            )
        )
        interval = max(interval, 0.05)
        # Never ask for more ticks than we have frames — that would duplicate.
        min_interval = duration / max(len(chronological), 1)
        interval = max(interval, min_interval)

        chosen: list[dict] = []
        used_images: set[str] = set()
        tick = 0.0
        while tick < duration:
            nearest = min(
                chronological, key=lambda f: abs(float(f.get("timestamp") or 0.0) - tick)
            )
            image = str(nearest.get("image"))
            if image not in used_images:
                used_images.add(image)
                chosen.append(nearest)
            tick += interval
        return [self._tile(ctx, f, segments) for f in chosen]

    def _transcript_tiles(
        self, ctx: PipelineContext, chronological: list[dict], segments: list[dict]
    ) -> list[TileSpec]:
        """One representative frame per spoken line — the view that answers
        'did the visual match the instruction?'."""
        if not segments or not chronological:
            return []
        tiles: list[TileSpec] = []
        for i, seg in enumerate(segments):
            start = seg.get("start")
            end = seg.get("end")
            if start is None or end is None:
                continue
            midpoint = (float(start) + float(end)) / 2.0
            nearest = min(
                chronological, key=lambda f: abs(float(f.get("timestamp") or 0.0) - midpoint)
            )
            timestamp = float(nearest.get("timestamp") or 0.0)
            within_segment = float(start) <= timestamp <= float(end)
            distance = max(float(start) - timestamp, timestamp - float(end), 0.0)
            label = f"SEG {i:02d}" if within_segment else f"SEG {i:02d} | NEARBY +{distance:.1f}s"
            tile = self._tile(ctx, nearest, segments, subtitle=label)
            # Retain the target line, but explicitly label an image outside
            # that line's interval instead of implying simultaneous evidence.
            tile.caption = seg.get("text")
            if not within_segment:
                tile.caption = f"[Nearby frame, {distance:.1f}s outside speech] {tile.caption or ''}"
            tile.extra["transcript_alignment"] = {
                "within_segment": within_segment,
                "distance_to_segment_seconds": round(distance, 3),
                "distance_to_midpoint_seconds": round(abs(timestamp - midpoint), 3),
            }
            tile.extra["transcript_segment"] = {
                "index": i,
                "start": start,
                "end": end,
                "text": seg.get("text"),
            }
            tiles.append(tile)
        return tiles

    def _key_moment_tiles(
        self,
        ctx: PipelineContext,
        settings,
        chronological: list[dict],
        segments: list[dict],
        sheet_capacity: int,
    ) -> list[TileSpec]:
        """A short highlight reel from the strongest signals we actually have:
        scene changes, annotated events, transcript boundaries, and visual
        difference between consecutive frames."""
        if len(chronological) <= 3:
            return [self._tile(ctx, f, segments) for f in chronological]

        # The summary is deliberately ONE sheet — capping at the sheet's own
        # capacity keeps the promised single key_moments_storyboard.jpg.
        limit = max(1, min(int(settings.STORYBOARD_KEY_MOMENTS_MAX), sheet_capacity))
        scored: dict[str, tuple[float, dict]] = {}

        def bump(frame: dict, score: float) -> None:
            key = str(frame.get("image"))
            current = scored.get(key)
            if current is None or score > current[0]:
                scored[key] = (score, frame)

        # Always keep the opening and the final payoff.
        bump(chronological[0], 1000.0)
        bump(chronological[-1], 999.0)

        # Annotated events and scene transitions are explicit signals.
        previous_scene = None
        for frame in chronological:
            if frame.get("event_id"):
                bump(frame, 900.0)
            scene = frame.get("scene_id")
            if scene is not None and scene != previous_scene:
                bump(frame, 800.0)
                previous_scene = scene

        # First frame of each transcript segment: a new instruction is spoken.
        seen_segments: set[int] = set()
        for frame in chronological:
            idx, _ = _segment_at(segments, float(frame.get("timestamp") or 0.0))
            if idx is not None and idx not in seen_segments:
                seen_segments.add(idx)
                bump(frame, 700.0)

        # Visual change between consecutive frames, via the same average-hash
        # the extractor already uses for near-duplicate suppression.
        try:
            from app.pipeline.steps.extract_frames import _average_hash

            previous_hash = None
            for frame in chronological:
                path = _frame_path(ctx, frame)
                if path is None or not path.is_file():
                    continue
                current = _average_hash(str(path))
                if current is None:
                    continue
                if previous_hash is not None:
                    distance = bin(current ^ previous_hash).count("1")
                    bump(frame, float(distance))
                previous_hash = current
        except Exception as exc:  # noqa: BLE001 - scoring is best-effort
            ctx.warning(f"Key-moment visual scoring unavailable ({exc}); using structural signals only")

        ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)[:limit]
        chosen = _sorted_by_time([frame for _, frame in ranked])
        return [self._tile(ctx, f, segments) for f in chosen]
