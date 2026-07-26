"""Builds output.zip by streaming files straight to disk (never buffered fully in memory).

Includes the v2 nested-dataset guard: stray ZIP archives and directories that
look like complete datasets of their own (their own manifest.json + source/)
are excluded from the export — with a warning and a machine-readable
exclusion record — instead of being silently packaged inside the new dataset.
Nothing is ever deleted from disk.
"""
import json
import os
import zipfile
from pathlib import Path

from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError


def _find_nested_dataset_roots(job_dir: Path) -> list[Path]:
    """Directories inside the job dir that look like a complete dataset of
    their own (accidentally copied in): they carry their own manifest.json
    alongside a source/ or transcripts/ directory."""
    nested = []
    for root, dirs, filenames in os.walk(job_dir):
        root_path = Path(root)
        if root_path == job_dir:
            continue
        if "manifest.json" in filenames and (
            (root_path / "source").is_dir() or (root_path / "transcripts").is_dir()
        ):
            nested.append(root_path)
            dirs.clear()  # no need to look deeper inside an excluded dataset
    return nested


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

        nested_roots = _find_nested_dataset_roots(Path(job_dir))
        exclusions: list[dict] = []
        for nested in nested_roots:
            rel = os.path.relpath(nested, job_dir)
            exclusions.append({"path": rel, "reason": "nested_dataset_directory"})
            ctx.warning(
                f"Excluding '{rel}/' from the export: it looks like a complete dataset of its own "
                "(has its own manifest.json). It was NOT deleted — remove or move it manually if unwanted."
            )

        all_files = []
        for root, dirs, filenames in os.walk(job_dir):
            root_path = Path(root)
            if any(root_path == n or n in root_path.parents for n in nested_roots):
                dirs.clear()
                continue
            for fname in filenames:
                full = os.path.join(root, fname)
                if full == str(zip_path):
                    continue
                arcname = os.path.relpath(full, job_dir)
                if fname.lower().endswith(".zip"):
                    exclusions.append({"path": arcname, "reason": "nested_zip_archive"})
                    ctx.warning(
                        f"Excluding '{arcname}' from the export: ZIP archives are never packaged "
                        "inside a dataset. It was NOT deleted."
                    )
                    continue
                all_files.append((full, arcname))

        if exclusions:
            exclusions_bytes = json.dumps(exclusions, indent=2).encode()
            ctx.storage.save_bytes(ctx.job_relative("metadata", "zip_exclusions.json"), exclusions_bytes)
            all_files.append(
                (str(ctx.storage.get(ctx.job_relative("metadata", "zip_exclusions.json"))), "metadata/zip_exclusions.json")
            )

        if not all_files:
            raise PipelineFailedError("zip_empty", "No output files were produced to archive.")

        total = len(all_files)
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                seen_arcnames: set[str] = set()
                for i, (full, arcname) in enumerate(all_files):
                    ctx.check_cancel()
                    if arcname in seen_arcnames:
                        continue
                    zf.write(full, arcname)
                    seen_arcnames.add(arcname)
                    ctx.set_step_progress(self.name, round(100 * (i + 1) / total))
        except OSError as exc:
            raise PipelineFailedError("zip_failed", f"Failed to build output.zip: {exc}") from exc

        ctx.info(f"output.zip created with {total} files ({zip_path.stat().st_size} bytes)")
        ctx.shared["zip_relative_path"] = zip_rel_path
        ctx.set_step_progress(self.name, 100)
