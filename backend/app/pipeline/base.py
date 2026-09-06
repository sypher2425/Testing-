"""Common interface for every pipeline step.

Adding a new step later (OCR, object detection, embeddings, etc.) means
subclassing PipelineStep, declaring what it consumes/produces, and appending
it to the ordered list in app.pipeline.runner.PIPELINE. No other code needs
to change.
"""
from abc import ABC, abstractmethod

from app.pipeline.context import PipelineContext


class PipelineStep(ABC):
    #: Must match one of the states in app.models.PIPELINE_STEPS
    name: str
    #: Human-readable label shown in the UI step indicator
    label: str
    #: Logical artifact keys this step reads from ctx.shared
    consumes: tuple[str, ...] = ()
    #: Logical artifact keys this step writes to ctx.shared
    produces: tuple[str, ...] = ()

    @abstractmethod
    def run(self, ctx: PipelineContext) -> None:
        """Execute the step. Must call ctx.set_step_progress(self.name, pct)
        periodically and raise on failure (exceptions are caught by the runner
        and turned into a failed job with a typed error)."""
