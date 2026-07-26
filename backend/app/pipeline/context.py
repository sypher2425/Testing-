"""Shared mutable state passed between pipeline steps."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.storage.base import StorageBackend


class JobCancelled(Exception):
    """Raised internally when a cancellation request is observed mid-step."""


@dataclass
class PipelineContext:
    job_id: str
    storage: StorageBackend
    options: dict[str, Any]
    log: Callable[[str, str], None]  # (level, message) -> None
    set_step_progress: Callable[[str, int], None]  # (step_name, 0-100) -> None
    should_cancel: Callable[[], bool]
    update_job: Callable[[dict[str, Any]], None]  # persist scalar fields onto the Job row
    # Refreshes last_heartbeat without touching progress — for long blocking
    # calls (model download, transcription) that would otherwise look dead to
    # the stale-job reaper.
    heartbeat: Callable[[], None] = lambda: None
    # Shared scratch space that later steps read from earlier steps.
    # e.g. shared["video"] = {"duration": .., "width": .., "has_audio": ..}
    shared: dict[str, Any] = field(default_factory=dict)

    def job_relative(self, *parts: str) -> str:
        return str(Path(self.job_id, *parts))

    def check_cancel(self) -> None:
        if self.should_cancel():
            raise JobCancelled(f"Job {self.job_id} was cancelled")

    def info(self, message: str) -> None:
        self.log("info", message)

    def warning(self, message: str) -> None:
        self.log("warning", message)

    def error(self, message: str) -> None:
        self.log("error", message)
