"""Evidence preservation, local-only inference, reuse and cancellation contracts."""
import io
import json
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings
from app.pipeline.context import JobCancelled
from app.pipeline.steps.analyze_visuals import AnalyzeVisualsStep
from app.utils.local_analysis import AnalysisCache, OCRWorker, cache_key, local_ollama_url, select_frames
from tests.test_pipeline_steps import make_ctx


def _context(tmp_path, monkeypatch, **options):
    settings = get_settings()
    monkeypatch.setattr(settings, "LOCAL_ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    ctx, _, _ = make_ctx(tmp_path / "jobs", options=options)
    image = Image.new("RGB", (640, 240), "white")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    ctx.storage.save_bytes(ctx.job_relative("frames", "frame.png"), buf.getvalue())
    ctx.shared.update({"original_filename": "example.mp4", "video": {"duration_seconds": 1800},
                       "frames": [{"timestamp": 12.5, "image": "frame.png", "frame": 0}],
                       "transcript": {"language": "en", "segments": [
                           {"start": 12, "end": 14, "text": "A complete first line.", "words": [{"word": "A", "start": 12, "end": 12.1}]},
                           {"start": 1795, "end": 1798, "text": "The final line must not be truncated."},
                       ]}})
    return ctx


def _timeline(ctx):
    return json.loads(ctx.storage.get(ctx.job_relative("analysis", "timeline.json")).read_text(encoding="utf-8"))


def test_complete_transcript_survives_optional_engines_disabled(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch, ocr_enabled=False, vision_enabled=False)
    with patch("app.pipeline.steps.analyze_visuals.OCRWorker.recognize") as recognize:
        AnalyzeVisualsStep().run(ctx)
    recognize.assert_not_called()
    timeline = _timeline(ctx)
    assert timeline["transcript"] == ctx.shared["transcript"]
    assert timeline["ocr"]["status"] == "skipped"
    assert timeline["items"][0]["transcript_segment_indices"] == [0]
    assert timeline["coverage"]["largest_visual_gap_seconds"] == 1787.5
    report = ctx.storage.get(ctx.job_relative("analysis", "report.txt")).read_text(encoding="utf-8-sig")
    assert "The final line must not be truncated." in report
    assert "00:29:55.000" in report


def test_ocr_reuses_exact_frame_and_settings_cache(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    recognized = {"status": "success", "lines": [{"text": "Export", "confidence": .96, "box": [[1, 2], [4, 2], [4, 8], [1, 8]]}]}
    with patch("app.pipeline.steps.analyze_visuals.OCRWorker.recognize", return_value=recognized) as recognize:
        AnalyzeVisualsStep().run(ctx)
        AnalyzeVisualsStep().run(ctx)
        assert recognize.call_count == 1
        assert _timeline(ctx)["ocr"]["cache_hits"] == 1
        assert _timeline(ctx)["items"][0]["ocr"]["lines"] == recognized["lines"]
        monkeypatch.setattr(get_settings(), "OCR_MIN_CONFIDENCE", .7)
        AnalyzeVisualsStep().run(ctx)
        assert recognize.call_count == 2


def test_missing_ocr_and_ollama_produce_partial_report(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch, vision_enabled=True)
    with patch("app.pipeline.steps.analyze_visuals.OCRWorker.recognize", side_effect=RuntimeError("OCR dependency missing")), \
         patch("app.pipeline.steps.analyze_visuals.find_local_model", side_effect=ConnectionError("Ollama unavailable")):
        AnalyzeVisualsStep().run(ctx)
    timeline = _timeline(ctx)
    assert timeline["status"] == "partial"
    assert timeline["ocr"]["status"] == timeline["vision"]["status"] == "unavailable"
    assert len(timeline["warnings"]) == 2
    assert "final line" in ctx.storage.get(ctx.job_relative("analysis", "report.md")).read_text()


def test_cancel_ocr_propagates_and_closes_native_worker(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    with patch("app.pipeline.steps.analyze_visuals.OCRWorker.recognize", side_effect=JobCancelled("cancelled")), \
         patch("app.pipeline.steps.analyze_visuals.OCRWorker.close") as close:
        with pytest.raises(JobCancelled):
            AnalyzeVisualsStep().run(ctx)
        close.assert_called_once()
    assert not ctx.storage.exists(ctx.job_relative("analysis", "timeline.json"))


def test_vision_cache_respects_objective_and_unloads_model(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch, ocr_enabled=False, vision_enabled=True, analysis_objective="Read the menu")
    observation = {"status": "success", "summary": "A menu is visible", "observations": ["An Export item appears"],
                   "visible_text": ["Export"], "uncertainty": ["The selected option is unclear"]}
    with patch("app.pipeline.steps.analyze_visuals.find_local_model", return_value={"name": "qwen3.5:4b", "digest": "one"}), \
         patch("app.pipeline.steps.analyze_visuals.describe_visual", return_value=observation) as describe, \
         patch("app.pipeline.steps.analyze_visuals.ollama_request", return_value={}) as unload:
        AnalyzeVisualsStep().run(ctx)
        AnalyzeVisualsStep().run(ctx)
        assert describe.call_count == 1
        assert _timeline(ctx)["vision"]["cache_hits"] == 1
        ctx.options["analysis_objective"] = "Describe the layout"
        AnalyzeVisualsStep().run(ctx)
        assert describe.call_count == 2
        assert unload.call_args.kwargs["payload"]["keep_alive"] == 0


@pytest.mark.parametrize("url", ["https://api.example.com", "http://8.8.8.8", "http://localhost@evil.example", "http://localhost:11434/api"])
def test_reject_external_or_ambiguous_vision_endpoints(url):
    with pytest.raises(ValueError):
        local_ollama_url(url)


def test_local_endpoints_and_corrupt_cache_are_safe(tmp_path):
    assert local_ollama_url("http://ollama:11434/") == "http://ollama:11434"
    assert local_ollama_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    key = cache_key("ocr", {"digest": "test"})
    cache = AnalysisCache(tmp_path)
    cache.write(key, {"status": "success"})
    (tmp_path / key[:2] / f"{key}.json").write_text("not json")
    assert cache.read(key) is None


def test_budget_covers_timeline_despite_dense_opening():
    frames = [{"timestamp": i / 10, "image": str(i)} for i in range(80)]
    frames += [{"timestamp": second, "image": str(second)} for second in range(60, 1801, 60)]
    chosen = select_frames(frames, 8)
    assert len(chosen) == 8
    assert chosen[0]["timestamp"] == 0
    assert chosen[-1]["timestamp"] == 1800
    assert sum(frame["timestamp"] < 8 for frame in chosen) <= 2


def test_storyboard_labels_frame_outside_segment(tmp_path):
    from app.pipeline.steps.storyboards import StoryboardStep

    ctx, _, _ = make_ctx(tmp_path)
    frames = [{"timestamp": 0, "image": "frame.jpg", "frame": 0}]
    tiles = StoryboardStep()._transcript_tiles(ctx, frames, [{"start": 10, "end": 12, "text": "Click Export"}])
    assert tiles[0].extra["transcript_alignment"]["within_segment"] is False
    assert tiles[0].extra["transcript_alignment"]["distance_to_segment_seconds"] == 10
    assert "Nearby frame" in tiles[0].caption


def test_manifest_lists_reports_and_preserves_analysis_on_rebuild(tmp_path, monkeypatch):
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx = _context(tmp_path, monkeypatch, ocr_enabled=False)
    ctx.shared.update({"mode": "adaptive", "stored_source_filename": "video.mp4",
                       "source_relative_path": ctx.job_relative("source", "video.mp4")})
    ctx.storage.save_bytes(ctx.shared["source_relative_path"], b"video")
    for filename in ("transcript.txt", "transcript.json", "subtitles.srt"):
        ctx.storage.save_bytes(ctx.job_relative("transcript", filename), b"transcript")
    AnalyzeVisualsStep().run(ctx)
    expected = ctx.shared.pop("analyses")
    GenerateMetadataStep().run(ctx)
    manifest = ctx.shared["manifest"]
    assert manifest["analyses"] == expected
    assert {"analysis/timeline.json", "analysis/report.md", "analysis/report.txt"} <= {entry["path"] for entry in manifest["files"]}


def test_cancel_interrupts_waiting_local_http_request():
    import asyncio
    from app.utils.local_analysis import ollama_request

    cancelled = []
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)
    checks = []
    def check_cancel():
        checks.append(True)
        if len(checks) > 1:
            raise JobCancelled("cancelled")
    with patch("httpx.AsyncClient", return_value=Client()):
        with pytest.raises(JobCancelled):
            ollama_request("http://localhost:11434", "/api/tags", check_cancel=check_cancel, timeout=30)
    assert cancelled == [True]


def test_real_cpu_ocr_returns_text_and_source_pixel_boxes(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    image = Image.new("RGB", (1000, 240), "white")
    draw = ImageDraw.Draw(image)
    candidates = ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    from pathlib import Path
    font_path = next((path for path in candidates if Path(path).exists()), None)
    if font_path is None:
        pytest.skip("A large test font is unavailable")
    font = ImageFont.truetype(font_path, 62)
    draw.text((45, 75), "EXPORT VIDEO 123", fill="black", font=font)
    image_path = tmp_path / "ocr.png"
    image.save(image_path)
    worker = OCRWorker(800, .5, 2)
    try:
        result = worker.recognize(image_path, check_cancel=lambda: None, timeout=90)
    finally:
        worker.close()
    assert "EXPORT" in " ".join(line["text"] for line in result["lines"]).upper()
    assert result["image_width"] == 1000
    assert result["box_coordinate_space"] == "source_frame_pixels"
    assert all(0 <= x <= 1000 and 0 <= y <= 240 for line in result["lines"] for x, y in line["box"])
