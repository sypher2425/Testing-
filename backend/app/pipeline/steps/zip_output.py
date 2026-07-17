"""Builds output.zip by streaming files straight to disk (never buffered fully in memory)."""
import os
import zipfile

from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError


class ZipOutputStep(PipelineStep):
    name = "zipping"
    label = "Building ZIP archive"
    consumes = ("manifest",)
    produces = ("zip",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        job_dir = ctx.storage.get(ctx.job_relative(""))
        zip_rel_path = ctx.job_relative("output.zip")
        zip_path = ctx.storage.get(zip_rel_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        all_files = []
        for root, _dirs, filenames in os.walk(job_dir):
            for fname in filenames:
                full = os.path.join(root, fname)
                if full == str(zip_path):
                    continue
                arcname = os.path.relpath(full, job_dir)
                all_files.append((full, arcname))

        if not all_files:
            raise PipelineFailedError("zip_empty", "No output files were produced to archive.")

        total = len(all_files)
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i, (full, arcname) in enumerate(all_files):
                    ctx.check_cancel()
                    zf.write(full, arcname)
                    ctx.set_step_progress(self.name, round(100 * (i + 1) / total))
        except OSError as exc:
            raise PipelineFailedError("zip_failed", f"Failed to build output.zip: {exc}") from exc

        ctx.info(f"output.zip created with {total} files ({zip_path.stat().st_size} bytes)")
        ctx.shared["zip_relative_path"] = zip_rel_path
        ctx.set_step_progress(self.name, 100)
