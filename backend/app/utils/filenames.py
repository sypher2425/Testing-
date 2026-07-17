"""Filename sanitization and deterministic frame naming."""
import re

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, *, fallback: str = "upload") -> str:
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("_", name)
    name = name.lstrip(".") or fallback
    return name[:255]


def frame_filename(timestamp_seconds: float, fmt: str = "jpeg") -> str:
    """Zero-padded seconds with milliseconds, e.g. '0003.500.jpg'."""
    ext = "jpg" if fmt == "jpeg" else "png"
    whole = int(timestamp_seconds)
    millis = round((timestamp_seconds - whole) * 1000)
    if millis == 1000:
        whole += 1
        millis = 0
    return f"{whole:04d}.{millis:03d}.{ext}"


def is_safe_relative_path(path: str) -> bool:
    """Reject any path containing traversal segments or absolute roots."""
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    parts = re.split(r"[\\/]+", path)
    return ".." not in parts
