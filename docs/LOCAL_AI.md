# Local processing and visual evidence

Frame AI 0.2 adds GPU transcription, batch frame decoding, configurable frame budgets, original-timeline frame ranges, dense bursts, local OCR, optional local vision, and reusable analysis jobs.

## This workstation

The Windows/WSL2 workstation has an NVIDIA RTX 3060 Ti (8 GB), Ryzen 7 5700X and 32 GB RAM. `docker-compose.override.yml` grants the worker and Ollama GPU access. `.env` enables the CUDA build, selects automatic Whisper device detection, and limits the worker to one concurrent job. Ollama is internal to the Compose network; it exposes no public port.

```powershell
docker compose up -d --build
docker compose exec -T ollama ollama pull qwen3.5:4b
```

The model download is needed only once. Named model-cache volumes survive normal rebuilds. Do not use `docker compose down -v` unless intentionally removing model and application data.

On a CPU-only host, use `docker compose -f docker-compose.yml up -d --build` and set `ENABLE_CUDA=false`, `WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE_TYPE=int8`. GPU access and model/runtime readiness appear in `GET /api/capabilities` and the website.

## Choosing settings

| Control | Behavior |
|---|---|
| Fast | Keyframe-based visual prescan, lighter speech search, up to 80 OCR images and 4 vision images |
| Balanced | Broader visual prescan, stronger speech search, up to 240 OCR images and 12 vision images |
| Detailed | Denser prescan, retained word timings, up to 600 OCR images and 24 vision images |
| Main frame budget | Default 2,000; maximum 20,000. Dense opening and event groups are additional evidence |
| Adaptive target | Default 300; 30–2,000 frames, bounded by the main budget |
| Frame range | Selects visual evidence from part of the source; speech transcription still covers the complete audio |
| Bursts | Up to eight original-timeline intervals at 1–60 requested FPS, sharing the main frame budget |
| OCR | Local CPU RapidOCR with bundled PP-OCR models; strongest for Chinese and English |
| Local vision | Optional Qwen3.5 4B; describes selected still images, with uncertainty and source references |

Requested rates may be reduced to the source rate or frame budget. The preflight estimate is planning information; final source timestamps and extraction metadata describe what was actually produced. Interval spacing is a sampling request, not generated/interpolated video. Every-frame mode iterates decoded source frames and handles variable source rates.

Fast prescans intentionally use software keyframe skipping: the tested NVIDIA CUVID decoder ignored that skip request and decoded the full source. Selected output images still use the GPU. Balanced/Detailed prescans inspect more images and therefore take longer.

Vision inference is deliberately selective. It cannot establish continuous motion or guarantee capture of a brief event between sampled images. Readable text often benefits more from a close crop or a selected detailed range than from additional overview sheets.

## Results and reuse

Every video job writes an evidence timeline and readable reports:

- `analysis/timeline.json`: complete transcript, frame references, OCR boxes/confidence, local visual observations, actual coverage and provenance.
- `analysis/report.md` and `analysis/report.txt`: readable combined speech and visual evidence.
- Main AI dataset downloads include available analysis reports and the frame index. Overview-sheet downloads also include full-size images that support OCR or vision observations.

Start with the readable report when supplying material to another AI; the JSON is an alternative structured representation of the same evidence. Use the referenced images for details instead of asking the model to infer tiny text from overview sheets.

**Analyze existing frames** creates a new job from a completed dataset's source, frames and transcript. It preserves the original and avoids transcription/extraction. Changing the analysis objective reruns relevant visual interpretation; cached OCR and unchanged observations can be reused. New processing jobs reuse transcripts when source hashes and transcription settings match. Reusing frames cannot recover information absent from the earlier extraction; upload/reprocess with a denser target or targeted range when needed.

## Runtime and maintenance

Whisper uses faster-whisper 1.2.1/CTranslate2 with bounded GPU batches. Model instances remain process-local to avoid unsafe reuse across Celery forks. Detailed profiles retain word timestamps; faster profiles omit that extra alignment work. Platform captions can skip Whisper for URL-based full datasets, with speech recognition as fallback.

OCR runs in a cancellable CPU subprocess. Vision runs through the local Ollama service, rejects remote/cloud models, limits output/context, and unloads GPU weights at the end of a step. Worker concurrency one prevents competing large model loads. App results distinguish unavailable/partial visual analysis from successful observations.

Processing caches are keyed by source/image content plus relevant inference settings/model versions. They expire after `ANALYSIS_CACHE_RETENTION_HOURS` (168 by default), independently of original job retention. Cache entries are advisory; failures to save them do not destroy a result.

No paid API account is required. Hardware, electricity, storage and initial software/model downloads are still local operating costs. Model-size labels do not equal runtime VRAM requirements.
