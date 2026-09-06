"""Expire only processing-cache files, independently of original job artifacts."""
import time
from pathlib import Path


def cleanup_processing_caches(settings) -> int:
    cutoff = time.time() - settings.ANALYSIS_CACHE_RETENTION_HOURS * 3600
    roots = [settings.data_path / "transcript_cache",
             Path(settings.LOCAL_ANALYSIS_CACHE_DIR) if settings.LOCAL_ANALYSIS_CACHE_DIR else settings.data_path / "analysis_cache"]
    removed = 0
    for directory in roots:
        root = directory.resolve()
        if not root.is_dir():
            continue
        for file in root.rglob("*.json"):
            try:
                if file.is_symlink() or not file.resolve().is_relative_to(root):
                    continue
                if file.is_file() and file.stat().st_mtime < cutoff:
                    file.unlink()
                    removed += 1
            except OSError:
                continue
    return removed
