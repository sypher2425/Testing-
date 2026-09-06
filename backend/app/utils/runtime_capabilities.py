"""Sanitized worker capability snapshot shared with the API container."""
import ctypes
import importlib.util
import json
import os
import time
from pathlib import Path

import httpx

from app.config import get_settings


def publish_worker_capabilities():
    settings = get_settings()
    gpu = {"available": False, "device": None}
    try:
        import ctranslate2
        available = ctranslate2.get_cuda_device_count() > 0
        if available:
            ctypes.CDLL("cublas64_12.dll" if os.name == "nt" else "libcublas.so.12")
            ctypes.CDLL("cudnn64_9.dll" if os.name == "nt" else "libcudnn.so.9")
        gpu = {"available": available, "device": "NVIDIA CUDA" if available else None}
    except (ImportError, RuntimeError, OSError) as exc:
        gpu["reason"] = str(exc)
    device = settings.WHISPER_DEVICE
    if device == "auto":
        device = "cuda" if gpu["available"] else "cpu"
    data = {"gpu": gpu, "transcription": {"model": settings.WHISPER_MODEL_SIZE,
            "device": device, "batch_size": settings.WHISPER_BATCH_SIZE if device == "cuda" else 1},
            "updated_at": time.time()}
    try:
        settings.data_path.mkdir(parents=True, exist_ok=True)
        temporary = settings.data_path / f"worker-runtime-{os.getpid()}.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, settings.data_path / "worker-runtime.json")
    except OSError:
        pass
    return data


def capabilities():
    settings = get_settings()
    try:
        runtime = json.loads((settings.data_path / "worker-runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        runtime = {"gpu": {"available": False, "device": None},
                   "transcription": {"model": settings.WHISPER_MODEL_SIZE, "device": "unverified",
                                     "batch_size": settings.WHISPER_BATCH_SIZE}}
    vision_available = False
    try:
        from app.utils.local_analysis import local_ollama_url
        base = local_ollama_url(settings.OLLAMA_BASE_URL)
        response = httpx.get(f"{base}/api/tags", timeout=2, trust_env=False)
        response.raise_for_status()
        names = {m.get("name") or m.get("model") for m in response.json().get("models", [])}
        vision_available = settings.OLLAMA_VISION_MODEL in names
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return {"version": settings.APP_VERSION, "max_frames": settings.MAX_FRAMES, "min_interval_ms": 17,
            "default_target_frames": settings.DEFAULT_TARGET_FRAMES, **runtime,
            "analysis": {"ocr_available": importlib.util.find_spec("rapidocr_onnxruntime") is not None,
                         "vision_available": vision_available, "vision_model": settings.OLLAMA_VISION_MODEL}}
