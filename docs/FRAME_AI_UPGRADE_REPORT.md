# Frame AI 0.2 — implementation report

Completed September 6, 2026 at 11:16 AM Singapore time.

The upgraded website is running at http://localhost:3000. Final deployment checks
confirmed API version 0.2.0, CUDA transcription, local OCR/vision readiness, and
that the running worker contains the verified final source files.
[Deployment verification](verification/deployment.json).

## Result

The website now has free local GPU speech recognition, faster frame extraction,
larger frame budgets, selected time ranges and dense bursts, local OCR, optional
visual AI, a searchable evidence timeline, combined report downloads, and analysis
reuse. The installed visual model is Qwen3.5 4B through local Ollama. No paid API
account is required.

The first complete long-video benchmark processed the existing **28-minute
29-second, 1440p60 AV1 source in 4 minutes 57 seconds**, excluding download time.
The previous recorded processing time was approximately **23 minutes 5 seconds**
after its download: **4.7× faster** on this workstation.

## Measured performance

| Work | Previous recorded run | Upgrade benchmark |
|---|---:|---:|
| Speech transcription | 658.8 seconds | 28.8 seconds |
| Frame extraction | 716.7 seconds | 262.0 seconds |
| Storyboards | 7.6 seconds | 3.7 seconds |
| ZIP creation | 1.9 seconds | 0.1 seconds |
| Processing excluding download | About 1,385 seconds | 296.52 seconds |

The new run completed at **08:01:17 AM SGT on September 6**. That timestamp is
the benchmark milestone, not the completion time of all subsequent fixes and tests.

This comparison uses the same retained source video, the small Whisper model,
and similar output counts: previously 81 main frames plus 32 opening frames;
the upgrade produced 80 plus 32. OCR and visual AI were disabled for this timing
comparison because the previous version did not perform those analyses. The
previous download took 231.5 seconds; the benchmark reused the local source.
Model weights were already downloaded. Transcription was newly computed, not a
transcript-cache hit. Hardware: RTX 3060 Ti 8 GB, Ryzen 7 5700X, 32 GB RAM.

This is a practical before/after observation, not an accuracy-controlled benchmark
or a promise for every 30-minute video. Speech text was present across the full
timeline; the two engines/settings produced approximately 5,920 versus 5,839 words.
No independently labelled transcript was available to measure word error rate.
More frames, higher resolution, OCR and vision add work.

The timing above predates the final explicit AV1 hardware-decoder improvement.
Decoder-specific verification is recorded separately so its effect is not confused
with transcription, download or model-cache savings.

The final isolated GPU check took **245.566 seconds** for the complete Balanced
prescan and 80 main frames, with 81 successful `av1_cuvid` decoder operations,
zero CPU fallbacks and zero warnings. This excludes opening frames, transcription,
download, analysis and exports. The prescan remains the largest cost. A separate
20-second/100-frame batch took 5.183 seconds on CPU and 3.746 seconds on GPU.
See [GPU decoder evidence](verification/gpu-decoder-benchmark.json).

**Fast profile reduced that same extraction-only workload to 52.426 seconds**
for 80 main frames, compared with Balanced's 245.566 seconds: 4.68× faster at
this stage. Fast's keyframe prescan took 4.43 seconds. The final implementation
uses CPU keyframe skipping for that scan because CUVID ignored the skip request;
all 80 output-frame operations still used the GPU. This is a separate stage
benchmark, not an end-to-end 52-second processing claim. No hardware failures
occurred. [Fast benchmark evidence](verification/fast-gpu-decoder-benchmark.json).

Raw stage evidence: [long-video-benchmark.json](verification/long-video-benchmark.json).

## What was lacking and what changed

- **Unused GPU:** enabled CUDA transcription with bounded batches and automatic
  CPU fallback. Model caching remains process-local for Celery reliability.
- **Repeated video decoding:** added bounded batch extraction, codec-aware NVIDIA
  decoding with CPU retry, and FFmpeg visual prescans that work with AV1. Actual
  source timestamps and decoder/fallback provenance are retained.
- **Small and inflexible sampling limits:** raised the global main-frame ceiling
  from 2,000 to 20,000, exposed a per-job budget, added ranges and up to eight
  1–60 FPS bursts, and made adaptive targets control the actual plan. Dense bursts
  share the main budget; opening/event evidence is additional.
- **Little visual interpretation:** added local RapidOCR with text boxes and
  confidence, and selective Qwen visual observations guided by your objective.
  Speech, text and observations remain tied to their source timestamps/images.
- **Hard-to-use exports:** added one combined readable report plus structured JSON,
  searchable speech/OCR/vision results, and full-resolution evidence images in
  overview downloads. Existing transcript export behavior is preserved.
- **Repeated work:** identical sources/settings can reuse transcripts. Reanalysis
  creates a separate job from existing frames and speech, preserving the original.
  Content-based OCR and visual caches avoid unchanged inference.
- **Unclear limits and progress:** added local runtime readiness, preflight frame
  estimates, actual output-rate reporting, bounded inference, cancellation,
  cache expiry, and explicit partial/unavailable analysis states.

## Recommended settings for a 30-minute video

Start with **Fast, adaptive target 300, OCR enabled**, and visual AI disabled when
turnaround is the priority. Give a concrete objective, such as “capture every
visible setting change and the speaker's explanation.” Enable local vision when
you need descriptions of images or scenes beyond their readable text.

Use **Balanced or Detailed** for information-rich screen recordings or when Fast
misses short changes. Detailed retains word timestamps and considers a denser
visual prescan. Use selected ranges and short 30–60 FPS bursts for fast actions.
Reanalysis can reinterpret saved evidence quickly; recovering an unsampled event
requires a fresh extraction of that range.

| Requested sampling over 30 minutes | Main images before overlaps/limits |
|---|---:|
| 1 FPS | 1,800 |
| 5 FPS | 9,000 |
| 10 FPS | 18,000 |
| 60 FPS | 108,000 — exceeds the 20,000 main-frame ceiling |

Higher sampling does not create details absent from the source. The budget and
source FPS can lower the achieved rate; the results show actual coverage. Full
60 FPS is most useful for a selected short range. OCR and vision analyze bounded
subsets of saved frames: Fast up to 80/4, Balanced 240/12, Detailed 600/24 images.
These are OCR/vision budgets, not claims that every saved image was analyzed.

## Free local components and operating notes

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper): local speech
  recognition with CTranslate2 and batching.
- [RapidOCR](https://github.com/RapidAI/RapidOCR): local OCR, with the installed
  ONNX runtime implementation and bundled recognition models.
- [Qwen3.5 4B in Ollama](https://ollama.com/library/qwen3.5:4b): a local multimodal
  model; the installed quantized download is about 3.4 GB.
- [NVIDIA FFmpeg decoding](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html):
  hardware decoding through codec-specific CUVID support, with software fallback.

Local inference avoids per-request API fees. It still uses your electricity,
disk and GPU. Ollama is internal to Docker, and this integration rejects remote
or cloud model destinations. OCR can misread small/stylized text; visual AI can
misinterpret a still image and cannot prove continuous motion between samples.
Use the included source images and uncertainty notes to check important claims.

The application remains a local workstation service. Source videos and generated
jobs still follow the existing 72-hour retention setting; processing caches expire
after seven days by default. Model downloads survive normal Docker rebuilds.
See [LOCAL_AI.md](LOCAL_AI.md) for setup, controls and maintenance.

## Verification

- **486 Linux backend tests passed**, one warning, 44.49 seconds. Coverage includes
  real AV1, variable frame rates, timestamps, cancellation, decoder fallback,
  cache reuse/invalidation, original-file preservation, validation, and safe ZIP
  evidence references. [Test log](verification/backend-tests.log).
- Frontend TypeScript checks and production build passed. Docker frontend builds
  now use the committed dependency lock through `npm ci`, avoiding dependency
  resolution on each build.
- All eight frontend planning tests passed, including 30-minute budget reductions,
  high-rate short ranges, burst limits and unknown-duration handling.
- Browser upload, range/budget planning, invalid-range blocking, search, report
  download, empty states, mobile layout and partial-analysis handling passed.
  A 390-pixel viewport had no horizontal overflow; no browser JavaScript errors
  were observed in the tested flows.
- **Real 60 FPS extraction passed:** a two-second range produced 120 distinct,
  increasing source timestamps (0.015–1.998 seconds). The website displayed
  120 main frames at 60 FPS, plus eight separately labelled opening frames.
  The job completed in 5.225 seconds with speech-cache reuse and OCR/vision off.
  [High-FPS evidence](verification/60fps-final-verification.json).
- **Real local visual analysis passed:** job `d0d5a2d0-2b03-433d-b774-fe2cb6c61ede`
  completed in **23.07 seconds**, reusing 62 OCR results and newly analyzing all
  four requested images with Qwen. Each returned valid structured evidence on its
  first attempt. Reanalysis skipped download, transcription and extraction. The
  original job and analysis hash remained identical.
- The browser downloaded both AI dataset formats. The frames ZIP contained 62
  images and no sheets; the overview ZIP contained two sheets plus 61 images
  supporting observations. Both included TXT/MD/JSON analysis, all observation
  image references resolved, and there were no duplicate ZIP entries.

Visual response truncation was found during the live test and fixed before final
deployment: the model now has a bounded concise schema, a sufficient token budget,
strict completion validation and one compact retry within the original deadline.
Incomplete output is reported as unavailable rather than treated as evidence.

## GitHub synchronization validation

After the implementation above, the user's requested GitHub pull incorporated six
upstream commits through `0ba9bb7` on `claude/video-ai-dataset-webapp-4divqc`.
The transcript-library merge preserves the default merged TXT with timestamps and
skipped-item summaries, alongside optional merged JSON and ZIP formats. Upstream
download cleanup, proxy configuration and TikTok diagnostics are retained. Proxy
credentials are now redacted from timeout/error details, with regression coverage.

The combined source passed **510 Linux backend tests**, one warning, in 37.77
seconds, with the merged dependency requirements installed. Frontend typecheck and
production build also passed. [Integration test log](verification/github-integration-tests.log).
The benchmark and live-browser records above describe the earlier upgrade
deployment; this additional check validates the source after the GitHub merge.
