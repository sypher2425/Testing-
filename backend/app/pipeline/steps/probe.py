from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.ffmpeg import FFmpegError, ffprobe


class ProbeStep(PipelineStep):
    name = "probing"
    label = "Probing video"
    produces = ("video",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 5)
        source_rel = ctx.shared["source_relative_path"]
        source_path = str(ctx.storage.get(source_rel))

        try:
            result = ffprobe(source_path)
        except FFmpegError as exc:
            raise PipelineFailedError("probe_failed", exc.message, exc.to_detail()) from exc

        ctx.info(
            f"Probed video: duration={result.duration_seconds:.2f}s "
            f"resolution={result.width}x{result.height} fps={result.fps} "
            f"codec={result.codec} has_audio={result.has_audio}"
        )

        video_meta = {
            "duration_seconds": result.duration_seconds,
            "width": result.width,
            "height": result.height,
            "fps": result.fps,
            "codec": result.codec,
            "has_audio": result.has_audio,
        }
        ctx.shared["video"] = video_meta
        ctx.update_job(video_meta)
        ctx.set_step_progress(self.name, 100)
