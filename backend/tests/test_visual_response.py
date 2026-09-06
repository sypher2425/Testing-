"""Bounded local vision output, retry, and cancellation regressions."""
import json
from unittest.mock import patch

import pytest
from PIL import Image

from app.pipeline.context import JobCancelled
from app.utils.local_analysis import (
    COMPACT_VISION_SCHEMA,
    VISION_SCHEMA,
    describe_visual,
)


@pytest.fixture()
def frame(tmp_path):
    path = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32), "white").save(path)
    return path


def _reply(**fields):
    result = {"summary": "An Export button is visible.", "observations": ["A menu is open."],
              "visible_text": ["Export"], "uncertainty": []}
    result.update(fields)
    return {"done": True, "done_reason": "stop", "eval_count": 72,
            "message": {"content": json.dumps(result)}}


def _describe(frame, **kwargs):
    return describe_visual(frame, base_url="http://localhost:11434", model="qwen3.5:4b",
                           objective="Read the screen", transcript_context="", check_cancel=kwargs.pop("check_cancel", lambda: None),
                           timeout=kwargs.pop("timeout", 120), **kwargs)


def test_complete_visual_response_has_bounded_schema_and_generation_provenance(frame):
    with patch("app.utils.local_analysis.ollama_request", return_value=_reply()) as request:
        result = _describe(frame)
    request.assert_called_once()
    payload = request.call_args.kwargs["payload"]
    assert payload["format"] == VISION_SCHEMA
    assert payload["options"]["num_predict"] == 1200
    assert payload["options"]["num_ctx"] == 4096
    assert payload["think"] is False
    assert result["summary"] == "An Export button is visible."
    assert result["generation"]["attempts"] == 1
    assert result["generation"]["output_tokens"] == 72
    assert result["generation"]["compact_retry"] is False


@pytest.mark.parametrize("truncated", [
    {"done": True, "done_reason": "length", "message": {"content": '{"summary":"broken'}},
    {"done": True, "done_reason": "stop", "message": {"content": '{"summary":"broken'}},
    {**_reply(), "done_reason": "length"},
    {**_reply(), "done": False},
])
def test_truncated_output_regenerates_once_from_original_image(frame, truncated):
    with patch("app.utils.local_analysis.ollama_request", side_effect=[truncated, _reply()]) as request:
        result = _describe(frame)
    assert request.call_count == 2
    first, second = [call.kwargs["payload"] for call in request.call_args_list]
    assert first["format"] == VISION_SCHEMA
    assert second["format"] == COMPACT_VISION_SCHEMA
    assert first["messages"][1] == second["messages"][1]
    assert "broken" not in json.dumps(second)
    assert result["generation"]["attempts"] == 2
    assert result["generation"]["compact_retry"] is True


@pytest.mark.parametrize("invalid", [
    _reply(summary=""),
    _reply(summary="a" * 301),
    _reply(observations=["one", "two", "three", "four"]),
    _reply(visible_text=[42]),
    _reply(uncertainty="unknown"),
    _reply(invented_field="unsupported"),
])
def test_invalid_or_overlong_observation_is_not_silently_accepted(frame, invalid):
    with patch("app.utils.local_analysis.ollama_request", side_effect=[invalid, _reply()]) as request:
        result = _describe(frame)
    assert request.call_count == 2
    assert result["generation"]["compact_retry"] is True


def test_second_truncation_fails_without_repair_or_further_retry(frame):
    truncated = {"done": True, "done_reason": "length", "message": {"content": '{"summary":"invented'}}
    with patch("app.utils.local_analysis.ollama_request", return_value=truncated) as request:
        with pytest.raises(ValueError, match="after 2 attempts"):
            _describe(frame)
    assert request.call_count == 2


def test_retry_uses_remaining_original_deadline(frame):
    with patch("app.utils.local_analysis.time.monotonic", side_effect=[10.0, 10.0, 90.0]), \
         patch("app.utils.local_analysis.ollama_request", side_effect=[{"message": {"content": "invalid"}}, _reply()]) as request:
        _describe(frame, timeout=120)
    assert [call.kwargs["timeout"] for call in request.call_args_list] == [120, 40]


def test_exhausted_time_budget_does_not_start_retry(frame):
    with patch("app.utils.local_analysis.time.monotonic", side_effect=[10.0, 10.0, 131.0]), \
         patch("app.utils.local_analysis.ollama_request", return_value={"message": {"content": "invalid"}}) as request:
        with pytest.raises(TimeoutError, match="per-frame time budget"):
            _describe(frame, timeout=120)
    request.assert_called_once()


def test_cancellation_between_attempts_prevents_retry(frame):
    with patch("app.utils.local_analysis.ollama_request", return_value={"message": {"content": "invalid"}}) as request:
        checks = iter([None, None, JobCancelled("cancelled")])
        def check_cancel():
            result = next(checks)
            if result is not None:
                raise result
        with pytest.raises(JobCancelled):
            _describe(frame, check_cancel=check_cancel)
    request.assert_called_once()


def test_network_failure_is_not_retried_as_output_failure(frame):
    with patch("app.utils.local_analysis.ollama_request", side_effect=TimeoutError("request timed out")) as request:
        with pytest.raises(TimeoutError, match="request timed out"):
            _describe(frame)
    request.assert_called_once()
