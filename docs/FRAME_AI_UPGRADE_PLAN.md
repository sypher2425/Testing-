# Frame AI implementation plan

Authorized 2026-09-06: implement, install free local dependencies/models, rebuild and verify the local website, and deliver a report. Preserve existing jobs and uncommitted work.

1. Repair AV1 scene decoding; batch dense extraction, preserve actual presentation timestamps, honor adaptive targets, frame budgets and selected time ranges.
2. Configure this PC's NVIDIA GPU for the Docker worker. Upgrade faster-whisper, use bounded batches, preserve worker-local model caches, retain detailed word timings, and reuse captions/transcripts.
3. Add bounded local OCR and optional local Ollama visual analysis, with explicit coverage, source references, uncertainty, content caching, and readable report downloads.
4. Add processing presets, requested/effective sampling estimates, frame/range/burst controls, local capability status, and searchable evidence results.
5. Integrate compatible manifests, cancellation, cache cleanup, progress throttling and efficient archives.
6. Run meaningful regression tests and browser checks; benchmark the existing approximately 28.5-minute AV1 source without deleting or changing the original job. Report measured gains separately from limitations.

All inference remains local. No paid API, account creation or external video upload is required. Baseline user edits are backed up under `.git/codex-backups/frame-ai-20260906`.
