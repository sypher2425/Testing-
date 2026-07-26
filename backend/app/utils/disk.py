import shutil

from app.config import get_settings


def free_disk_mb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 * 1024)


def ensure_enough_disk(path: str, incoming_mb: float, *, multiplier: float | None = None) -> None:
    """Raise ValueError if free space would be dangerously low after accepting
    an upload of incoming_mb, based on MIN_FREE_DISK_MB headroom.

    A source video costs more than its own size: extracted frames, the temp
    audio.wav, and output.zip all land on the same volume. `multiplier`
    (default UPLOAD_DISK_HEADROOM_MULTIPLIER) accounts for those derived
    artifacts, so a 60GB upload is rejected up front rather than failing
    partway through the pipeline with ENOSPC. Pass multiplier=1.0 to check
    the raw size only.
    """
    settings = get_settings()
    factor = settings.UPLOAD_DISK_HEADROOM_MULTIPLIER if multiplier is None else multiplier
    free_mb = free_disk_mb(path)
    needed_mb = incoming_mb * factor
    required = needed_mb + settings.MIN_FREE_DISK_MB
    if free_mb < required:
        detail = f"upload {incoming_mb:.0f}MB"
        if factor != 1.0 and incoming_mb > 0:
            detail += f" x{factor:g} for derived artifacts (frames, audio, zip) = {needed_mb:.0f}MB"
        raise ValueError(
            f"Not enough free disk space: {free_mb:.0f}MB available, "
            f"{required:.0f}MB required ({detail} + "
            f"{settings.MIN_FREE_DISK_MB}MB safety margin)."
        )
