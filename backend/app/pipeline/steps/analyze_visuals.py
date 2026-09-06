"""Bounded local visual evidence, complete transcript, and one readable report."""
from bisect import bisect_right
import json
from pathlib import Path
import time

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import JobCancelled, PipelineContext
from app.utils.local_analysis import (
    ANALYSIS_VERSION, OCR_MODEL, AnalysisCache, OCRWorker, cache_key,
    describe_visual, file_digest, find_local_model, local_ollama_url, ollama_request, select_frames,
)
from app.utils.timeouts import HeartbeatTicker
from app.utils.timestamps import now_utc_iso

ARTIFACTS = {"timeline": "analysis/timeline.json", "report_markdown": "analysis/report.md",
             "report_text": "analysis/report.txt"}


def _stamp(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render_report(timeline: dict, *, markdown: bool = True) -> str:
    """All transcript segments are retained; sampled evidence has explicit scope."""
    heading = "# " if markdown else ""
    lines = [f"{heading}Video evidence report", "", f"Source: {timeline['source_title']}",
             f"Generated: {timeline['generated_at']}", f"Status: {timeline['status']}",
             f"Objective: {timeline['objective'] or 'General video understanding'}", ""]
    coverage = timeline["coverage"]
    lines += [f"{heading}Coverage", "", f"Extracted frames: {coverage['extracted_frames']}",
              f"Visual range: {_stamp(coverage['range_start_seconds'])} - {_stamp(coverage['range_end_seconds']) if coverage['range_end_seconds'] is not None else 'unknown end'}",
              f"OCR: {timeline['ocr']['status']} ({timeline['ocr']['processed_frames']} frames)",
              f"Visual observations: {timeline['vision']['status']} ({timeline['vision']['processed_frames']} frames)",
              "Observations cover sampled images only; events between them may be missing.", ""]
    for warning in timeline["warnings"] + coverage["limitations"]:
        lines.append(f"- {warning}")
    lines += ["", f"{heading}Complete transcript", ""]
    segments = timeline["transcript"].get("segments") or []
    if not segments:
        lines.append("No transcript is available: " + str(timeline["transcript"].get("skipped_reason") or "no speech detected"))
    for segment in segments:
        speaker = f" [{segment['speaker']}]" if segment.get("speaker") else ""
        lines.append(f"[{_stamp(float(segment.get('start') or 0))} - {_stamp(float(segment.get('end') or 0))}]{speaker} {segment.get('text', '')}")
    lines += ["", f"{heading}Timestamped visual evidence", ""]
    found = False
    for item in timeline["items"]:
        ocr, vision = item["ocr"], item["vision"]
        if not ocr.get("lines") and vision.get("status") != "success":
            continue
        found = True
        lines += [f"{heading}{_stamp(item['timestamp_seconds'])} | {item['source_frame']}", ""]
        for line in ocr.get("lines") or []:
            lines.append(f"Text ({line['confidence']:.0%} OCR score): {line['text']}")
        if vision.get("summary"):
            lines.append("Visual observation: " + vision["summary"])
        for text in vision.get("visible_text") or []:
            lines.append("Text read by vision model (may contain errors): " + text)
        for observation in vision.get("observations") or []:
            lines.append("- " + observation)
        for uncertainty in vision.get("uncertainty") or []:
            lines.append("Uncertain: " + uncertainty)
        lines.append("")
    if not found:
        lines.append("No visual text or model observations were produced. Full-resolution frames remain available in the dataset.")
    lines += ["", "The JSON timeline includes OCR boxes, scores, frame links, full transcript metadata, and inference provenance.", ""]
    return "\n".join(lines)


class AnalyzeVisualsStep(PipelineStep):
    name = "analyzing_visuals"
    label = "Reading visual evidence"
    consumes = ("frames", "transcript")
    produces = ("analyses",)

    def run(self, ctx: PipelineContext) -> None:
        settings = get_settings()
        ctx.check_cancel()
        ctx.set_step_progress(self.name, 0)
        frames = sorted(ctx.shared.get("frames") or [], key=lambda frame: float(frame.get("timestamp") or 0))
        transcript = ctx.shared.get("transcript") or {"segments": [], "skipped": True, "skipped_reason": "not_run"}
        profile = ctx.options.get("processing_profile") or "balanced"
        if profile not in {"fast", "balanced", "detailed"}:
            profile = "balanced"
        objective = str(ctx.options.get("analysis_objective") or "")[:2000]
        cache = AnalysisCache(Path(getattr(settings, "LOCAL_ANALYSIS_CACHE_DIR", "") or Path(settings.DATA_DIR) / "analysis_cache"))
        segments = transcript.get("segments") or []
        segment_order = sorted(range(len(segments)), key=lambda index: float(segments[index].get("start") or 0))
        starts = [float(segments[index].get("start") or 0) for index in segment_order]
        items = []
        for index, frame in enumerate(frames):
            timestamp = float(frame.get("timestamp") or 0)
            position = bisect_right(starts, timestamp) - 1
            aligned = []
            if position >= 0:
                segment_index = segment_order[position]
                if timestamp <= float(segments[segment_index].get("end") or 0):
                    aligned = [segment_index]
            items.append({"id": f"frame-{index}", "timestamp_seconds": timestamp,
                          "source_frame": f"frames/{frame['image']}", "scene_id": frame.get("scene_id"),
                          "transcript_segment_indices": aligned,
                          "ocr": {"status": "not_sampled", "lines": []},
                          "vision": {"status": "not_sampled"}})
        item_by_image = {frame["image"]: item for frame, item in zip(frames, items)}
        warnings = []
        ocr = {"enabled": ctx.options.get("ocr_enabled", True), "status": "skipped",
               "engine": "RapidOCR ONNX CPU", "model": OCR_MODEL, "selected_frames": 0,
               "processed_frames": 0, "cache_hits": 0, "text_lines": 0}
        vision = {"enabled": bool(ctx.options.get("vision_enabled", False)), "status": "skipped",
                  "engine": "Ollama local", "model": getattr(settings, "OLLAMA_VISION_MODEL", "qwen3.5:4b"),
                  "selected_frames": 0, "processed_frames": 0, "cache_hits": 0}
        digests = {}
        with HeartbeatTicker(settings.HEARTBEAT_INTERVAL_SECONDS, ctx.heartbeat):
            if ocr["enabled"] and frames:
                budget = getattr(settings, f"OCR_MAX_FRAMES_{profile.upper()}", {"fast": 80, "balanced": 240, "detailed": 600}[profile])
                chosen = select_frames(frames, max(0, int(budget)))
                ocr["selected_frames"] = len(chosen)
                max_dim = int(getattr(settings, "OCR_MAX_DIM", 1600))
                confidence = float(getattr(settings, "OCR_MIN_CONFIDENCE", 0.5))
                worker = OCRWorker(max_dim, confidence, int(getattr(settings, "OCR_CPU_THREADS", 2)))
                deadline = time.monotonic() + float(getattr(settings, "OCR_TIMEOUT_SECONDS", 1800))
                try:
                    for index, frame in enumerate(chosen):
                        ctx.check_cancel()
                        if time.monotonic() >= deadline:
                            raise TimeoutError("The OCR time budget was reached; completed evidence is retained")
                        path = ctx.storage.get(ctx.job_relative("frames", frame["image"]))
                        digest = digests.setdefault(frame["image"], file_digest(path))
                        key = cache_key("ocr", {"frame_sha256": digest, "model": OCR_MODEL,
                                               "max_dim": max_dim, "min_confidence": confidence})
                        result = cache.read(key)
                        cached = result is not None and result.get("status") == "success"
                        if not cached:
                            result = worker.recognize(path, check_cancel=ctx.check_cancel,
                                                      timeout=min(120, max(1, deadline - time.monotonic())))
                            cache.write(key, result)
                        item_by_image[frame["image"]]["ocr"] = {**result, "cached": cached, "frame_sha256": digest}
                        ocr["processed_frames"] += 1
                        ocr["cache_hits"] += int(cached)
                        ocr["text_lines"] += len(result.get("lines") or [])
                        ctx.set_step_progress(self.name, round(65 * (index + 1) / max(1, len(chosen))))
                    ocr["status"] = "success"
                except JobCancelled:
                    raise
                except Exception as exc:
                    ocr["status"] = "partial" if ocr["processed_frames"] else "unavailable"
                    ocr["reason"] = f"{type(exc).__name__}: {exc}"
                    warnings.append("OCR: " + ocr["reason"])
                    ctx.warning(warnings[-1])
                finally:
                    worker.close()
            else:
                ocr["reason"] = "Disabled by request" if not ocr["enabled"] else "No extracted frames"
            ctx.set_step_progress(self.name, 65)
            if vision["enabled"] and frames:
                model = None
                try:
                    base_url = local_ollama_url(getattr(settings, "OLLAMA_BASE_URL", "http://host.docker.internal:11434"))
                    model = find_local_model(base_url, vision["model"], check_cancel=ctx.check_cancel)
                    vision["model_digest"] = model["digest"]
                    budget = getattr(settings, f"VISION_MAX_FRAMES_{profile.upper()}", {"fast": 4, "balanced": 12, "detailed": 24}[profile])
                    chosen = select_frames(frames, max(0, int(budget)))
                    vision["selected_frames"] = len(chosen)
                    for index, frame in enumerate(chosen):
                        ctx.check_cancel()
                        item = item_by_image[frame["image"]]
                        path = ctx.storage.get(ctx.job_relative("frames", frame["image"]))
                        digest = digests.get(frame["image"]) or file_digest(path)
                        nearby_text = " ".join(segments[i].get("text") or "" for i in item["transcript_segment_indices"])
                        key = cache_key("vision", {"frame_sha256": digest, "model": model,
                                                  "objective": objective, "transcript_context": nearby_text[:1600]})
                        result = cache.read(key)
                        cached = result is not None and result.get("status") == "success"
                        if not cached:
                            result = describe_visual(path, base_url=base_url, model=model["name"], objective=objective,
                                                     transcript_context=nearby_text, check_cancel=ctx.check_cancel,
                                                     timeout=float(getattr(settings, "OLLAMA_TIMEOUT_SECONDS", 120)))
                            cache.write(key, result)
                        item["vision"] = {**result, "cached": cached, "frame_sha256": digest}
                        vision["processed_frames"] += 1
                        vision["cache_hits"] += int(cached)
                        ctx.set_step_progress(self.name, 65 + round(30 * (index + 1) / max(1, len(chosen))))
                    vision["status"] = "success"
                except JobCancelled:
                    raise
                except Exception as exc:
                    vision["status"] = "partial" if vision["processed_frames"] else "unavailable"
                    vision["reason"] = f"{type(exc).__name__}: {exc}"
                    warnings.append("Local vision: " + vision["reason"])
                    ctx.warning(warnings[-1])
                finally:
                    if model is not None:
                        # Release the shared GPU before the next job's Whisper.
                        # Cleanup also runs after a cancelled inference request.
                        try:
                            ollama_request(base_url, "/api/generate", payload={"model": model["name"], "keep_alive": 0},
                                           check_cancel=lambda: None, timeout=5)
                        except Exception:
                            ctx.warning("Could not immediately unload the local vision model; Ollama's short keep-alive remains in effect")
            else:
                vision["reason"] = "Disabled by request" if not vision["enabled"] else "No extracted frames"
        ctx.check_cancel()
        duration = (ctx.shared.get("video") or {}).get("duration_seconds")
        frame_range = ctx.shared.get("frame_range") or {}
        range_start = float(frame_range.get("start_seconds", ctx.options.get("range_start_seconds")) or 0)
        range_end = frame_range.get("end_seconds", ctx.options.get("range_end_seconds")) or duration
        times = [range_start] + [item["timestamp_seconds"] for item in items]
        if range_end is not None:
            times.append(float(range_end))
        times.sort()
        limitations = ["OCR and vision inspect selected still frames, not every source frame. Unseen actions cannot be established.",
                       "OCR confidence is a recognition score, not a guarantee of correct text. Bundled recognition is strongest for Chinese and English."]
        if ocr["enabled"] and ocr["selected_frames"] < len(frames):
            limitations.append(f"The {profile} profile selected {ocr['selected_frames']} of {len(frames)} frames for OCR.")
        if vision["enabled"]:
            limitations.append(f"Visual interpretation covered {vision['processed_frames']} individual sampled frames; it does not establish continuous motion.")
        if (ctx.shared.get("burst_config") or {}).get("budget_limited"):
            limitations.append("Requested burst sampling was reduced to preserve timeline coverage within the frame budget.")
        status = "partial" if any(engine["status"] in {"partial", "unavailable"} for engine in (ocr, vision)) else "success"
        timeline = {
            "schema_version": "1.0", "status": status, "generated_at": now_utc_iso(),
            "source_title": str(ctx.shared.get("original_filename") or ctx.job_id),
            "objective": objective, "processing_profile": profile,
            "coverage": {"source_duration_seconds": duration, "range_start_seconds": range_start,
                         "range_end_seconds": range_end, "extracted_frames": len(frames),
                         "ocr_selected_frames": ocr["selected_frames"], "ocr_processed_frames": ocr["processed_frames"],
                         "vision_selected_frames": vision["selected_frames"], "vision_processed_frames": vision["processed_frames"],
                         "largest_visual_gap_seconds": round(max((b - a for a, b in zip(times, times[1:])), default=0), 3),
                         "limitations": limitations},
            "transcript": transcript, "ocr": ocr, "vision": vision, "items": items,
            "warnings": warnings, "provenance": {"implementation": ANALYSIS_VERSION,
                "ocr_model": OCR_MODEL, "ocr_device": "cpu", "vision_model": vision["model"],
                "vision_model_digest": vision.get("model_digest"), "external_uploads": False,
                "source_video_sha256": ctx.shared.get("source_sha256"),
                "reused_from_job_id": ctx.shared.get("reused_from_job_id"),
                "extraction": {key: ctx.shared[key] for key in ("frame_range", "visual_scan", "adaptive_config", "burst_config", "interval_config") if key in ctx.shared},
                "timestamps": "seconds in original source video", "cache": "sha256 frame bytes + model + analysis settings"},
            "artifacts": ARTIFACTS,
        }
        ctx.storage.save_bytes(ctx.job_relative("analysis", "timeline.json"), json.dumps(timeline, indent=2, ensure_ascii=False, allow_nan=False).encode())
        ctx.storage.save_bytes(ctx.job_relative("analysis", "report.md"), render_report(timeline).encode("utf-8"))
        ctx.storage.save_bytes(ctx.job_relative("analysis", "report.txt"), render_report(timeline, markdown=False).encode("utf-8-sig"))
        ctx.shared["analyses"] = {key: value for key, value in timeline.items() if key not in {"items", "transcript"}}
        ctx.info(f"Evidence report: OCR {ocr['processed_frames']} frames ({ocr['cache_hits']} cached), vision {vision['processed_frames']} frames ({vision['cache_hits']} cached)")
        ctx.set_step_progress(self.name, 100)
