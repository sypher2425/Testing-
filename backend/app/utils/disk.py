import shutil

from app.config import get_settings


def free_disk_mb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 * 1024)


def ensure_enough_disk(path: str, incoming_mb: float) -> None:
    """Raise ValueError if free space would be dangerously low after accepting
    an upload of incoming_mb, based on MIN_FREE_DISK_MB headroom."""
    settings = get_settings()
    free_mb = free_disk_mb(path)
    required = incoming_mb + settings.MIN_FREE_DISK_MB
    if free_mb < required:
        raise ValueError(
            f"Not enough free disk space: {free_mb:.0f}MB available, "
            f"{required:.0f}MB required (upload {incoming_mb:.0f}MB + "
            f"{settings.MIN_FREE_DISK_MB}MB safety margin)."
        )
