# Frame AI — Video → AI-Ready Dataset

Turn an uploaded video into a self-describing, AI-ready dataset — transcript,
representative frames, and a `manifest.json` an LLM can read to understand the
whole bundle without opening anything else. Version 0.2 adds GPU transcription
and decoding, budgeted adaptive sampling, up to 60 FPS for selected ranges,
local OCR and optional local visual AI, searchable evidence, and reusable analysis.
Adaptive mode defaults to 300 representative frames with a configurable target
of 30–2,000. Interval extraction supports a main frame budget up to 20,000.

See [local AI setup and usage](docs/LOCAL_AI.md) and the
[implementation and benchmark report](docs/FRAME_AI_UPGRADE_REPORT.md).

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- Space for Docker images, model weights and job artifacts; the optional
  Qwen visual model alone downloads about 3.4 GB
- The default Compose override enables NVIDIA GPUs. For CPU-only machines,
  use `docker compose -f docker-compose.yml up --build` and `ENABLE_CUDA=false`

## One-command startup

```bash
cp .env.example .env
docker compose up --build
```

- Frontend: http://localhost:3000
- API: http://localhost:8000 (docs at http://localhost:8000/docs)

Upload a video, pick an extraction mode (adaptive is selected by default),
watch live progress, and download the resulting `.zip` once it completes.

The first transcription job downloads the faster-whisper model weights (small
by default) and caches them inside the `worker` container; subsequent jobs
reuse the cached model.

## Architecture

```mermaid
flowchart LR
    subgraph Browser
        UI[Next.js frontend]
    end

    subgraph API_Container[api container]
        API[FastAPI]
    end

    subgraph Worker_Container[worker container]
        Celery[Celery worker + embedded beat]
        Pipeline[Pipeline steps: probe → transcribe →\nextract frames → metadata → zip]
    end

    Redis[(Redis\nbroker + result backend)]
    DB[(SQLite\njobs.db)]
    FS[(Local filesystem\n/data/jobs/&#123;job_id&#125;)]

    UI -- REST + SSE --> API
    API -- enqueue job --> Redis
    Redis -- deliver task --> Celery
    Celery --> Pipeline
    Pipeline -- ffmpeg / ffprobe / faster-whisper / PySceneDetect --> FS
    API -- read/write --> DB
    Celery -- read/write --> DB
    API -- serve frames/transcript/zip --> FS
```

### Services (`docker-compose.yml`)

| Service    | Role |
|------------|------|
| `frontend` | Next.js App Router UI |
| `api`      | FastAPI — accepts uploads, exposes job status/SSE/downloads, never blocks on processing |
| `worker`   | Celery worker running the pipeline, with an embedded beat scheduler for retention cleanup and stale-job reaping |
| `redis`    | Celery broker + result backend |

SQLite (`/data/db/app.db`) and job artifacts (`/data/jobs/{job_id}/...`) live
on a shared Docker volume mounted into both `api` and `worker`. The schema
uses plain SQLAlchemy types (no SQLite-specific features) so swapping
`DATABASE_URL` to Postgres later is a connection-string change, not a rewrite.

## How a job flows

```
queued → fetching_source → probing → loading_model → transcribing → extracting_frames → generating_metadata → zipping → completed
                                                                                                             ↘ failed / cancelled (from any state)
```

`fetching_source` is a fast no-op for a plain file upload (the file's already on
disk). For a pasted URL, this is where [URL ingestion](#url-ingestion-youtube-tiktok-instagram)
happens — metadata + video + comments via yt-dlp — before the rest of the
pipeline runs exactly as it would for an uploaded file.

Two other job types run shorter pipelines on the same machinery:
[transcript mode](#transcript-mode-any-link--a-transcript) (`fetching_source →
loading_model → transcribing → generating_metadata → zipping`) and
[research mode](#research-mode-topic-search--transcript-bundle) (`searching →
fetching_captions → generating_metadata → zipping`).

Each step reports 0-100% progress; the API streams updates over Server-Sent
Events (`GET /api/jobs/{id}/events`) with a polling fallback
(`GET /api/jobs/{id}` on an interval). A Celery beat task periodically checks
for jobs whose heartbeat has gone stale (e.g. the worker crashed mid-job) and
marks them `failed` with a clear reason — nothing is left "processing
forever".

### Transcription reliability

Transcription is the slowest and most memory-hungry step, so it has dedicated
safeguards:

- **`loading_model` is its own step.** The first run downloads ~460MB of
  Whisper weights (`small`). Folding that into "Transcribing audio" made a
  multi-minute download indistinguishable from a hang; now it has its own
  status, progress bar, and log lines.
- **The model cache is a named Docker volume** (`whisper_cache` →
  `/root/.cache/huggingface`). It survives `up --build` and `down`, so the
  download is a one-time cost — only `down -v` forces a re-download. Set
  `PREFETCH_WHISPER_MODEL=true` as a build arg to bake the weights into the
  image instead (offline-safe, ~460MB larger image).
- **The model is loaded once per worker and reused** across sequential jobs;
  the job log says explicitly whether it was loaded or reused.
- **Three layers of timeout**: `WHISPER_MODEL_LOAD_TIMEOUT_SECONDS` and
  `WHISPER_TIMEOUT_SECONDS` bound the two phases with typed failures,
  `HF_HUB_DOWNLOAD_TIMEOUT` bounds a stalled download socket, and Celery's
  `TASK_HARD_TIME_LIMIT_SECONDS` kills the child as a last resort. Nothing
  can hang indefinitely.
- **Progress advances per transcript segment** and a background heartbeat
  refreshes `last_heartbeat` during blocking calls, so the reaper can tell
  "slow" from "dead" and never fails a job that's actually working.
- **`TRANSCRIPTION_CONCURRENCY` defaults to 1.** Each concurrent transcription
  holds its own model copy; two at once on a memory-capped Docker Desktop can
  be OOM-killed, which strands a job in `transcribing`. Jobs queue instead,
  and the Processing view shows **"Waiting in queue — N jobs ahead"** so a
  waiting job never looks frozen. Raise the value if you have RAM headroom.
- **Abnormal worker deaths are explained, not silent.** A child killed by the
  OOM killer fails its job with `worker_out_of_memory` and a message naming
  the knobs to turn, instead of leaving the row untouched.
- **Interrupted jobs recover at startup.** On worker boot, jobs left in a
  running state are requeued (if the source is still on disk) or marked
  `failed` with `error_code: "interrupted"` — controlled by
  `STARTUP_RECOVERY_MODE`.

*Future optimization (not implemented):* a shared-model multithreaded worker
that serves one model instance to N threads would allow real parallelism
without N× memory. Deferred deliberately — the per-process model with
concurrency 1 is the stable configuration.

### Output layout (dataset schema v2)

```
{job_id}/
  source/video.<ext>
  transcript/transcript.txt
  transcript/transcript.json
  transcript/subtitles.srt
  frames/0000.000.jpg ...           # adaptive frames (flat, unchanged from v1)
  frames/opening_dense/...          # every 0.25s of the first 8s (configurable)
  frames/key_events/...             # ±0.25s around each annotated event
  storyboards/                      # labeled contact sheets (see Storyboards)
  storyboard_manifest.json          # maps every tile back to its source frame
  metadata/frames.json              # all frames w/ category, event_id, phash, transcript link
  analytics/performance.json        # engagement metrics + rates, each w/ status/source
  content/caption.txt               # exact post caption (URL jobs)
  content/audio.json                # word counts, overall vs narration WPM, silence gaps, music
  comments/top_comments.json        # full comment objects (id/author/likes/replies/pinned)
  comments/extraction_status.json   # WHY comments are (or aren't) there
  performance/comments.json         # legacy v1 shape, kept for backward compat
  events.json                       # manual-first timeline events (hook, twists, CTA...)
  manifest.json                     # dataset_schema_version: "2.2"
  output.zip
```

`manifest.json` is the entry point for AI consumption: job id, original
filename, video properties, detected language, extraction mode + params,
per-category frame counts, `source_video_sha256`, an `extraction_report`
(status + timing per stage), a `posting_context` block, a machine-readable
`analysis_summary` for cross-video comparison, every file with a relative
path + description, and an `analyses` section with local OCR and optional
visual observations. Timestamped reports live in `analysis/` — see
[Extending the pipeline](#extending-the-pipeline).

**The dataset never fabricates data.** Any metric that can't be extracted is
stored as `null` with an explicit status and reason, using this vocabulary:
`success · calculated · partial · not_available · unsupported ·
authentication_required · rate_limited · extraction_failed ·
unexpected_empty_result · manual_required · manual_unavailable ·
no_comments · comments_disabled · skipped · unknown`. `not_available` means
the platform genuinely doesn't have the metric (e.g. Instagram share counts);
`extraction_failed` means it exists but couldn't be read;
`unexpected_empty_result` means the platform reports N>0 but extraction
returned 0 (auth/rate-limit); `unsupported` means the current yt-dlp
extractor doesn't do that job (TikTok comment text); `no_comments` /
`comments_disabled` distinguish real emptiness from a fetch failure;
`calculated` marks values derived from other extracted values (rates);
`manual_required` means only the creator's own analytics can supply it —
those fields are filled via the enrichment flow (coming in the next round) or
manual entry. Every metric also carries a `precision` field (`exact`,
`rounded`, `estimated`, `screenshot_estimated`, `unknown`) so downstream
consumers can weigh comparisons correctly; yt-dlp values default to `exact`.
A v1 dataset (no `dataset_schema_version`) is still fully readable; every
v2 field is additive.

Old datasets: v1 job folders and ZIPs remain valid — all v1 files keep their
paths and shapes, and v2 files simply won't exist there. Consumers should
treat a missing `dataset_schema_version` as `1.0`.

### Schema v2.1 additions (round 1.5)

- `identity`: `{dataset_id, platform, platform_post_id, canonical_url,
  source_url_original, source_video_sha256, exported_at}`. Canonical URLs
  strip tracking params (`?is_from_webapp=1`, `?igsh=…`, `?si=…`) but keep
  the identifying path bits; `platform_post_id` is the extracted native
  ID (TikTok video id, Instagram shortcode, YouTube video id).
- `performance_snapshot`: `{fetched_at, metric_window, source, platform}`.
  Records **when** the metrics were retrieved so a dataset regenerated
  next week is distinguishable from an earlier capture of the same video.
- `platform_capabilities`: per-platform record of what the *current
  extractor* can retrieve (e.g. `tiktok.public_comment_text: false`,
  `instagram.public_views: false`). Describes our extractor, not the
  platform itself.
- Frame `mode` and `category` enums are now enforced. Adaptive frames use
  `mode: "adaptive"`, dense-opening frames use `mode: "dense_interval"`,
  key-event frames use `mode: "key_event"` — the R1 contradiction where
  dense frames had `mode: "adaptive"` is fixed. Valid combinations live
  in `app/utils/frame_schema.py`.
- Audio fields renamed for precision: `voiceover_span_seconds` (first
  spoken word → last spoken word), `active_narration_seconds` (sum of
  segment spans), `silence_or_render_wait_seconds` (span − narration),
  `overall_video_wpm`, `active_narration_wpm`. Old names
  (`voiceover_duration_seconds`, `overall_wpm`) preserved as deprecated
  aliases with a `_deprecated` note pointing to the new names.
- `extraction_params.opening_dense` and `.key_events` record the
  **effective** values used, with a `source` label
  (`default_configuration` / `user_interface`). No more `null` for a
  value the pipeline actually used.
- `total_frame_count` + `frame_count_legacy_meaning: "adaptive_frames_only"`
  disambiguate the `frame_count` legacy field.
- Every metric carries a `precision` field. yt-dlp values default to
  `exact`; rates get `precision: "derived_from_<worst-input>"` (no fake
  six-decimal precision when an input is only known as a rounded number).
- Every rate also carries `numerator_field` + `denominator_field`
  references and `status: "calculated"`.
- `metadata/validation_report.json` records the pre-export validator's
  status (`success` / `warning` / `error`), warnings list, errors list,
  and `validated_at`. Validation is report-only (the dataset still ships)
  but a dataset with any finding never reports a clean `success`.

### Schema v2.2 additions (round 1 stabilization)

**Canonical dense configuration.** `extraction_params.opening_dense`
(`{enabled, duration_seconds, interval_seconds, source}`) is the canonical
record of the dense-opening extraction that actually ran. The flat keys
`opening_dense_enabled` / `opening_dense_duration` / `opening_dense_interval`
are **deprecated**: they are still written for v1 readers, but always
back-filled from the canonical block, so they can never be `null` while
dense extraction is enabled and never disagree with it. An
`extraction_params._deprecated` map documents each one.

**Per-frame descriptions.** Every entry in `metadata/frames.json` (and the
matching `files[]` entry in the manifest) carries a `description` generated
from the frame's *own* metadata — mode, category, timestamp,
extraction_reason — via one shared helper (`app/utils/frame_schema.py:
describe_frame`), e.g. `Extracted opening-dense frame at t=2.25s using
dense interval mode.` A dense frame can no longer be described as adaptive;
the validator recomputes the same string and flags any disagreement.

**Timestamp policy.** All generated processing timestamps are ISO 8601 with
an explicit UTC offset (`2026-07-26T10:00:19.668895+00:00`), produced by the
single shared utility `app/utils/timestamps.py`. Naive timestamps found in
older datasets are normalized (assumed UTC) with a compatibility warning —
they never prevent a dataset from loading.

**Posting-date precision.** A calendar date is not an exact timestamp.
`posting_context.posted_at` is now `{value, status, source, entry_method,
precision, timezone}` with temporal precision from `exact_datetime · minute
· hour · date_only · month_only · unknown` (yt-dlp upload dates are
`date_only`, timezone `null`). When a more precise value is supplied later,
`upgrade_posted_at` accepts it only if strictly finer and preserves the
superseded record under `posted_at.provenance` — original provenance is
never lost.

**Source vs entry method.** `source` now means true provenance — `yt_dlp`
(scraped from the platform), `user` (a person supplied it), `computed`
(derived from other extracted data; e.g. all audio stats) — and the new
`entry_method` (`automatic` / `manual`) records how it got into the
dataset. The old `source: "auto"` label is deprecated: legacy datasets map
`auto → yt_dlp/computed + automatic` and `manual → user + manual` at read
time. `performance.fields_from` intentionally keeps its `auto`/`manual`
values — it is a stable UI contract.

**Silence analysis.** `content/audio.json` gains a `silence_analysis` block
that makes every number's scope explicit: `total_non_narration_seconds`
(scope `all_gaps_within_voiceover_span` — every non-speaking second between
the first and last spoken word), and `listed_periods` (scope
`full_video_gaps_over_threshold` — gaps ≥ the declared
`listed_periods_minimum_duration_seconds`, including lead-in and tail),
each with `duration_seconds`, plus `listed_periods_total_seconds`. The two
totals measure different things and now say so; the legacy
`silence_or_render_wait_seconds` / `silence_periods` keys remain as
deprecated aliases that always mirror the canonical values.

**Migration behavior.** `app/utils/manifest_compat.py` is the single home
for reading older datasets: `detect_schema_version` (missing → 1.0),
`normalize_manifest` (synthesizes the nested dense block from flat keys,
upgrades `posted_at`, normalizes naive timestamps — always in memory, with
warnings), and `normalize_frames` (backfills category/description on v1
frame lists). Old ZIPs and job folders are **never modified**; v1, v2.0,
and v2.1 datasets all remain loadable.

**Validation codes.** The validator emits stable codes. Structural
contradictions are *errors* on freshly generated (≥2.2) datasets and
*warnings* on legacy ones — either way the result is never `success`:

| Code | Meaning |
|---|---|
| `dense_config_null` | dense enabled but duration/interval null (always an error) |
| `dense_legacy_canonical_mismatch` | flat dense keys disagree with the canonical block |
| `dense_config_missing` | flat keys present but canonical block absent |
| `frame_description_mismatch` | a description disagrees with the frame's metadata |
| `dense_frame_described_as_adaptive` | the original R1 description bug |
| `incompatible_mode_category` / `invalid_frame_mode` / `invalid_frame_category` | frame enum violations |
| `frame_count_mismatch` | declared counts vs files on disk |
| `frame_category_count_mismatch` | declared counts vs frame metadata |
| `total_frame_count_mismatch` | total vs per-category sum |
| `naive_timestamp` | a generated stamp without a UTC offset |
| `posted_at_missing_precision` / `invalid_temporal_precision` / `posted_at_precision_value_mismatch` | posting-date precision issues |
| `silence_totals_ambiguous` | legacy silence keys with no scoped analysis block |
| `silence_listed_total_mismatch` / `silence_period_below_threshold` | listed periods don't reconcile |
| `deprecated_field_mismatch` | a deprecated alias contradicts its canonical replacement |
| `deprecated_source_label` | `source: "auto"` written by a ≥2.2 dataset |
| `invalid_entry_method` / `invalid_status` / `invalid_precision` / `silent_zero` | vocabulary violations |
| `missing_schema_version` / `missing_source_dir` / `missing_performance_snapshot` / `identity_inconsistent` / `audio_wpm_ordering` / `nested_dataset` | pre-existing checks, unchanged |

**Historical Round 1 scope:** enrichment, OCR and visual AI were deferred.
Version 0.2 now includes local OCR, local visual AI and adaptive novelty
selection; retention estimation, comment summarization and demographic
import remain future work.

## Storyboards

Hundreds of loose frames are hard for a model to reason about: it has to open
each one and rebuild the timeline itself. After extraction the pipeline
composes the frames into labeled contact sheets in `storyboards/`, so the
visual timeline can be read at a glance.

**Sheets are an index, not a detail view.** Vision models downscale large
images (a 2400px sheet typically becomes ~1568px), so a sheet packed with
tiles cannot also be the place to read small UI text. The tiles are sized for
timeline comprehension, and `storyboard_manifest.json` maps every tile back to
its full-resolution frame — which stays in the dataset untouched — for detail
work.

### The five views

| File | What it answers |
|---|---|
| `adaptive_storyboard_NN.jpg` | The representative pass over the whole video |
| `opening_dense_storyboard_NN.jpg` | The first seconds in detail — hook, first visible object, time to first interaction |
| `timeline_storyboard_NN.jpg` | An evenly spaced overview, independent of the adaptive algorithm |
| `transcript_storyboard_NN.jpg` | One frame per spoken line — did the visual match the instruction? |
| `key_moments_storyboard.jpg` | A single summary sheet of the biggest changes |

In `interval` / `per_second` modes the main frame series still carries
`category: "adaptive"` (that is the dataset's own vocabulary for "the main
series"), so its sheets are named `adaptive_storyboard_NN.jpg` regardless of
the sampling mode.

### Tiles and layout

Every tile carries a unique frame number, an exact `MM:SS.mmm` timestamp, the
transcript line active at that moment when one exists, and a scene or segment
label. Labels live in a dedicated caption strip **under** the frame, never over
it. Reading order is strictly left to right, then top to bottom.

Frames are letterboxed into the tile box — never cropped or stretched — and the
column count follows orientation: portrait video gets 5 columns, landscape 6,
because portrait tiles are tall and the same column count would double the
sheet height. Row count is additionally bounded by `STORYBOARD_MAX_SHEET_HEIGHT_PX`.
Typical results at defaults: a portrait sheet is 2400×3974 with 20 tiles at
463×823px; a landscape sheet is 2400×1450 with 24 tiles at 384×216px. Sheets
split automatically once the tiles overflow, and the last page is trimmed to
the rows it actually uses.

### Transcript captions

A tile is matched to the transcript segment active at its timestamp
(`start <= t < end`). The transcript-aligned view instead picks the frame
nearest each segment's midpoint and forces that segment's caption onto it, so a
frame that drifts into a neighbouring segment is still labeled with the line it
illustrates. Captions wrap to the caption strip and ellipsise only when they
must; **the complete text is always preserved in the manifest**. With no
transcript, storyboards still build — the transcript view is simply absent.

### storyboard_manifest.json

```json
{
  "status": "success",
  "video": { "filename": "clip.mp4", "durationSeconds": 32.0, "width": 1080, "height": 1920, "fps": 30.0 },
  "layout": { "sheet_width": 2400, "columns": 5, "tiles_per_sheet": 20, "tile_width": 463, "tile_height": 823, "captions": true },
  "types_built": ["adaptive", "opening_dense", "timeline", "transcript", "key_moments"],
  "storyboards": [
    {
      "type": "adaptive",
      "file": "storyboards/adaptive_storyboard_01.jpg",
      "sheet_index": 1, "sheet_count": 2, "columns": 5, "rows": 4,
      "frames": [
        {
          "tileIndex": 1, "frameNumber": 0,
          "timestampSeconds": 0.0, "timestampLabel": "00:00.000",
          "sourceFrame": "frames/0000.000.jpg",
          "transcriptSegment": { "index": 0, "start": 0.0, "end": 6.4, "text": "Add a goal counter at the top" }
        }
      ]
    }
  ]
}
```

### Regenerating

`POST /api/jobs/{id}/storyboards/regenerate` (the **Regenerate** button on the
results page) rebuilds the sheets from the frames already on disk — no
re-extraction, no re-transcription. Use it after changing layout settings or to
retry when storyboard generation failed on a job whose frames are fine. The
rebuild refreshes `storyboard_manifest.json`, `manifest.json` and `output.zip`,
and stamps `manifest.processing.last_rebuilt_at`.

### Failure behaviour

Storyboard generation is additive and never fails a job. A missing or corrupt
frame renders a labeled placeholder tile and is counted in
`unavailable_frames`; a composition error is recorded as a status in
`manifest.storyboards` with a pointer to regenerate, leaving the frames and
transcript untouched.

### Dense sampling (every 0.2s)

The **Dense — every 0.2s** preset in Advanced sets interval mode to 200ms.
Because `MAX_FRAMES` (2000) still applies, a video long enough to exceed it has
its interval **widened** rather than its tail dropped — the whole video stays
covered, a warning names both intervals in the job log, and
`extraction_params.interval` records `requested_interval_seconds` alongside
`effective_interval_seconds`. A 60-minute video asked for at 0.2s is sampled at
1.8s; nothing silently claims otherwise.

## API

All responses are JSON. Errors use a consistent envelope:

```json
{ "error": { "code": "not_found", "message": "Job ... not found", "detail": null } }
```

| Method | Path | Description |
|---|---|---|
| POST | `/api/jobs/upload` | **Streaming upload for files (any size).** Raw file bytes as the request body; options as query params (`filename` required, plus `mode`, `interval_ms`, `target_frames`, `frame_format`, `frame_max_dim`, `manual_*`). Written straight to disk in chunks — nothing buffered. Returns `{ job_id }` (202). 413 if over `MAX_UPLOAD_MB`, 507 if disk is short. |
| POST | `/api/jobs/transcript` | **Transcript only.** JSON `{ url, source_preference?, language? }`. Any platform yt-dlp supports; captions first, Whisper fallback. Returns `{ job_id }` (202). |
| GET | `/api/jobs/transcripts/bulk?ids=a,b,c&format=txt\|json\|srt&merged=` | **Every transcript from a batch in one download.** Default: a ZIP with one file per job, named by title. `merged=true`: a single combined .txt (headers per video) or .json array. Merged SRT is refused — concatenated subtitle timelines are corrupt. Unready jobs are skipped and listed in `skipped.txt` instead of sinking the download. |
| POST | `/api/jobs/transcript/upload` | **Transcript from a file.** Raw body, `filename` required (plus optional `language`). Accepts video *or* audio containers. Returns `{ job_id }` (202). |
| POST | `/api/jobs` | Multipart: either `file` or `url` (exactly one), plus options (`mode`, `interval_ms`, `target_frames`, `frame_format`, `frame_max_dim`) and optional `manual_*` performance overrides. Returns `{ job_id }` (202) immediately. Use this for URL ingestion; prefer `/api/jobs/upload` for files. |
| GET | `/api/jobs` | Paginated recent jobs |
| GET | `/api/jobs/{id}` | Full job status + per-step progress + error detail |
| GET | `/api/jobs/{id}/events` | SSE progress stream |
| GET | `/api/jobs/{id}/transcript?format=txt\|json\|srt` | Transcript in the requested format |
| GET | `/api/jobs/{id}/frames` | Paginated frame list with thumbnail URLs |
| GET | `/api/jobs/{id}/frames/{filename}` | Serve one frame image |
| GET | `/api/jobs/{id}/manifest` | Raw `manifest.json` |
| GET | `/api/jobs/{id}/logs?since_id=` | Persisted per-job log lines (used by the Processing view) |
| GET | `/api/jobs/{id}/video` | Source video with HTTP Range support, for the Results view's transcript-linked preview player |
| GET | `/api/jobs/{id}/download?asset=zip\|transcript\|frames\|storyboards` | Download the full dataset or a subset. Research jobs accept `zip` only; transcript jobs accept `zip` or `transcript`. |
| DELETE | `/api/jobs/{id}` | Cancel if running (revokes the Celery task) and delete all artifacts |

### Extraction modes

| Mode | Behavior |
|---|---|
| `adaptive` (default) | FFmpeg novelty preview with timeline coverage; 30–2,000 target frames and profile-dependent scan density. |
| `interval` | Requested frame spacing down to 17ms, bounded by source FPS and the main frame budget. |
| `per_second` | One frame per second |
| `every_frame` | Every decoded source frame, including variable-rate timing, within the selected range. Jobs exceeding the main frame budget are rejected with guidance to shorten the range or use interval sampling. Global maximum: 20,000 main frames. |

## URL ingestion (YouTube, TikTok, Instagram)

Paste a link instead of uploading a file and the `fetching_source` step uses
[yt-dlp](https://github.com/yt-dlp/yt-dlp) to pull the video down and feed it
into the exact same pipeline as an uploaded file — frame extraction,
transcription, and the manifest are all unaffected by where the source came
from.

Auto-fetched where the platform allows it, mapped into `manifest.json`'s
`performance` block: view/like/comment/share counts, title, description,
uploader, upload date, and hashtags. Top comments (by likes, capped at
`YTDLP_COMMENT_LIMIT`, default 100) are saved to `performance/comments.json` —
best-effort on TikTok/Instagram, since comment scraping there is more fragile
than on YouTube.

Any `manual_*` field supplied at upload time (`manual_view_count`,
`manual_title`, `manual_hashtags`, etc. — comma-separated for hashtags) always
overrides the auto-fetched value, and is the sole source when no URL is given
at all (a plain file upload can still carry manually-entered performance
data). `manifest.json`'s `performance.fields_from` records which fields came
from which source.

**Failure handling** — two tiers:
- Can't get the **video itself** (private, geo-blocked, deleted, or yt-dlp's
  extractor doesn't recognize the site) → the job fails with a typed error:
  `video_unavailable` (updating yt-dlp won't help) or `extractor_outdated`
  (it might).
- Can't get **metadata/comments** but the video downloaded fine → logged as a
  warning, falls back to manual fields (or nulls), and the job completes
  normally. This is never fatal.

**Keeping yt-dlp current without breaking reproducible builds:** the version
is pinned in `requirements.txt` like every other dependency — it is *never*
auto-updated on container startup. Instead, when an extraction fails in a way
that looks like a broken/outdated extractor (not a 404/private video), the
worker runs a one-time `pip install --upgrade yt-dlp`, logs the old and new
version, and retries the extraction exactly once. If the update itself fails
(no network, etc.) it's logged and the pinned version is used for that retry
— an update failure never crashes the job or the worker.

Extractor fixes for fast-moving sites (TikTok, Instagram) land on yt-dlp's
**nightly** channel days before they reach stable. Set `YTDLP_CHANNEL=nightly`
with `YTDLP_UPDATE_ON_STARTUP=true` to track it; the update runs at worker
boot, never inside a job.

**TLS browser impersonation.** Several extractors ask to impersonate a real
browser's TLS fingerprint, which needs `curl_cffi` — hence
`requirements.txt` installing `yt-dlp[default,curl-cffi]` rather than plain
`yt-dlp`. Verify it inside the worker with:

```bash
docker compose exec worker yt-dlp --list-impersonate-targets
```

Real rows (`Chrome-133  Macos-15  curl_cffi`) mean it's working; rows marked
`(unavailable)` mean the extra is missing — rebuild the image
(`docker compose build worker`). The worker probes this itself and warns when
a failed extraction coincides with no available target.

Note that impersonation is *not* the fix for TikTok's "universal data for
rehydration" error — that one has its own section below.

**Cookies for account-gated fetches** (mainly Instagram view counts, which
are often hidden from anonymous requests): drop a `cookies.txt` (exported
from your browser) at `secrets/cookies.txt` — that directory is bind-mounted
into the `worker` container at `/run/secrets` — and set
`COOKIES_FILE=/run/secrets/cookies.txt` in `.env`, then
`docker compose restart worker`. See `secrets/README.md` for the full
walkthrough. **Treat that file like a password** — it carries live session
tokens for whatever account you exported it from. Nothing requires it; leave
`COOKIES_FILE` blank if you don't need it, and a missing/misconfigured file
just falls back to anonymous requests rather than breaking every fetch.

**Platform limitations (what auto-extraction can and cannot get):**

| Metric | YouTube | TikTok | Instagram |
|---|---|---|---|
| Views / likes | ✅ | ✅ | likes ✅, views often withheld (`extraction_failed`) |
| Share/repost count | ❌ `not_available` (no public metric) | ✅ (repost count) | ❌ `not_available` (no public metric) |
| Comments (text) | ✅ reliable | ❌ `unsupported` by yt-dlp | fragile — often `authentication_required`/`rate_limited` |
| Saves, profile visits, follows, retention | ❌ creator-only analytics on every platform → `manual_required` |  |  |

`comments/extraction_status.json` records the exact outcome of every comment
extraction attempt — including the previously-confusing case where the
platform reports hundreds of comments but extraction returns zero
(`reason: "zero_results_unexpected"`).

## Transcript mode (any link → a transcript)

The Home page's **Transcript only** tab takes one link from any site yt-dlp
supports and returns just the transcript — no frames, no storyboards, and
**the video stream is never downloaded**.

It is built to be cheap. Most platforms already carry a caption track, and
fetching a VTT file costs a fraction of a second and no media bytes at all,
so the pipeline asks for captions before it asks for anything else:

```
fetching_source → loading_model → transcribing → generating_metadata → zipping
```

- **Captions found** → they are parsed into timed segments and the job is
  essentially done. `loading_model` is skipped outright: there is no point
  loading a ~460MB Whisper model to transcribe nothing.
- **No captions** → only the audio is downloaded (`-f ba/bestaudio`, not the
  video), and Whisper transcribes it as usual. On a long video that is the
  difference between a few MB and a few GB.

Downloaded audio is deleted the moment the transcript is written: it is
excluded from the ZIP, nothing in the UI plays it, and a batch of link jobs
would otherwise hold gigabytes until retention swept them hours later
(`TRANSCRIPT_DELETE_SOURCE_AFTER`, on by default). **Uploaded** files are
never deleted — that may be your only copy.

Either way the output is the same three files a video job produces —
`transcript/transcript.json`, `transcript.txt`, `subtitles.srt` — served by
the same `/api/jobs/{id}/transcript` endpoint. Platform captions are parsed
into the same `{start, end, text}` segments Whisper emits, so nothing
downstream has to care which path ran. `transcript.json` records which one
did, in `transcript_source` (`platform_captions` | `whisper`) and
`caption_track` (`manual` | `automatic` | null) — stated, not inferred.

**From a file instead of a link.** The Transcript tab has a second sub-tab
that takes a video *or audio* file directly (`POST /api/jobs/transcript/upload`,
the same streaming raw-body upload the dataset pipeline uses). There is no
platform involved, so it always runs Whisper.

This is the way through when a platform can't be extracted at all — save the
video yourself and drop it in. Audio-only files are accepted (`.mp3`, `.m4a`,
`.wav`, `.flac`, `.ogg`, `.opus`, …): there are no frames to extract here, so
an audio file is a perfectly good input. A file with **no** audio track fails
with `no_audio_track` rather than returning an empty transcript — unlike a
full dataset job, where a silent video still yields frames, there is nothing
else to produce.

**`source_preference`** (link only — a file has no captions to prefer):

| Value | Behaviour |
|---|---|
| `captions_first` (default) | Platform captions, Whisper fallback |
| `captions_only` | Never downloads audio; fails with `captions_unavailable` if the link has no caption track |
| `whisper_only` | Ignores platform captions entirely — useful when a platform's auto-captions are known to be bad |

`language` is an optional ISO-639-1 hint for which caption track to request
(`en`, `es`, `ja`, …). Whisper detects the language itself, so it only
affects the captions path.

`manifest.json` (`schema_version: "transcript-1.0"`) reports the transcript's
own coverage — `first_segment_start_seconds`/`last_segment_end_seconds` —
rather than assuming it spans the video: captions frequently stop early, and
a silent tail is a real result. An empty transcript reports nulls, not zeros.

The pipeline reuses the video pipeline's step names (a strict subset, in the
same order), so transcript jobs need no new job states, status chips or
progress labels.

### TikTok extraction

TikTok fails differently from everything else, and its own error text points
the wrong way:

```
ERROR: [TikTok] 7666…: Unable to extract universal data for rehydration;
please report this issue … Confirm you are on the latest version using yt-dlp -U
```

That message says "update yt-dlp". **Updating does not fix it.** The TikTok
extractor has been current since March 2026, and upstream tracks this as a
site-bug, not an extractor bug. What is actually happening: TikTok served a
bot-check page instead of the video data, so there was no embedded JSON to
parse.

yt-dlp has a second path — TikTok's **mobile API** — but it only attempts it
when it has app info. Without any, `_KNOWN_APP_INFO` is empty and it goes
straight to the web page that is being blocked:

```python
# yt_dlp/extractor/tiktok.py — TikTokIE._real_extract
if self._KNOWN_APP_INFO:
    try:
        return self._extract_aweme_app(video_id)   # mobile API
    except ExtractorError as e:
        self.report_warning(f'{e}; trying with webpage')
# ...falls through to the web page
```

So set `TIKTOK_DEVICE_ID` in `.env` to turn the mobile API on:

```bash
# TikTok app → Settings → scroll to the bottom → tap the version number 5×
TIKTOK_DEVICE_ID=1234567890123456789
```

```bash
docker compose up -d worker   # .env is read at container start; no rebuild needed
```

The web page stays as the fallback, so this only adds a path — it never
removes one. Two things worth knowing:

- **Cookies can hurt here.** The extractor solves a JS challenge and sets its
  own TikTok cookies; a stale jar from `COOKIES_FILE` can conflict with that.
  If TikTok is the only platform failing, try blanking `COOKIES_FILE` first —
  it costs nothing and rules out a whole class of cause.
- **This is IP-sensitive.** The same link can work from one network and get
  bot-checked from another, which is why upstream labels the issue
  `cant-reproduce`.

#### How an extraction failure is actually diagnosed

yt-dlp funnels wildly different situations into stderr that usually ends with
"Confirm you are on the latest version". Taking that literally is what once
labelled a current version `extractor_outdated`. So:

**Two attempts, then stop.** Attempt 1 uses the configured cookies plus
browser impersonation. If it fails in a way where credentials could plausibly
be the cause, attempt 2 repeats it anonymously after a short pause. That
comparison is the diagnosis — it is not available from stderr at all:

| Attempt 1 (cookies) | Attempt 2 (anonymous) | Verdict |
|---|---|---|
| blocked | succeeds | `tiktok_cookie_invalid` — replace the cookie export |
| blocked | blocked | `tiktok_region_restricted` — the server's IP is blocked |
| private/removed | (not attempted) | terminal; retrying cannot help |

There is no third attempt. TikTok rate-limits aggressively, and hammering it
turns a recoverable failure into a durable block.

**Categories** replace the old catch-all: `tiktok_layout_changed`,
`tiktok_bot_challenge`, `tiktok_cookie_invalid`, `tiktok_login_required`,
`tiktok_video_private`, `tiktok_video_unavailable`,
`tiktok_region_restricted`, `tiktok_rate_limited`, `tiktok_network_error`,
`yt_dlp_dependency_missing`, `yt_dlp_update_required`,
`source_extraction_unknown`. Each carries a message naming what to do, and
the UI shows which *stage* failed — a source download failing is a different
problem from transcription failing.

**yt-dlp is never updated during a job.** A mid-job `pip install` swapped the
binary under running work and never fixed the failure that triggered it.
Version management is now `YTDLP_CHANNEL` (`stable` | `nightly`) applied at
worker startup when `YTDLP_UPDATE_ON_STARTUP=true`, or by an explicit admin
action. The worker logs its version, channel, impersonation targets and
cookie age at boot.

#### When TikTok blocks the server's IP

The job log tells you which situation you are in:

```
Extraction failed with the configured cookies (tiktok_bot_challenge)
Both authenticated and anonymous extraction failed (tiktok_region_restricted)
```

Two failures, one with credentials and one without, mean the cookies are not
the *sole* cause. That is all it means — yt-dlp does not say the IP is
banned, so neither does the app. Work through these cheapest first:

**First, read which failure it is.** `tiktok_bot_challenge` means TikTok's
**JavaScript challenge** could not be solved — from yt-dlp's
`_solve_challenge_and_set_cookies`, the page carried neither the challenge
element nor the "Please wait..." interstitial. Cookies are irrelevant to that:
the challenge runs *before* they are used, so re-exporting them changes
nothing. Only `tiktok_cookie_invalid` and `tiktok_login_required` are actually
about your session.

**1. `TIKTOK_DEVICE_ID` — free, ~30 seconds.** TikTok's mobile API is a
different endpoint from the blocked web page, and yt-dlp only tries it when it
has app info. Open the TikTok app → Settings → scroll to the bottom → tap the
version number 5×.

```bash
TIKTOK_DEVICE_ID=1234567890123456789
docker compose up -d worker    # .env is read at container start
```

**2. `YTDLP_CHANNEL=nightly`** (with `YTDLP_UPDATE_ON_STARTUP=true`).
Challenge-solving is the part of the TikTok extractor that changes most often,
so its fixes reach nightly days before stable. Worth trying whenever the
failure is `tiktok_bot_challenge`.

**3. `YTDLP_PROXY` — only if the IP really is blocked.** Routes requests
through a different address. This is common on a **VPS or cloud host**, whose
ranges TikTok blocks wholesale, and rare on a home connection — so check where
the worker actually runs before paying for a residential proxy.

```bash
YTDLP_PROXY=http://user:pass@proxy.example:8080
```

Credentials in that value are redacted from job logs and from
`/api/health/extraction`.

**4. Upload the file.** Always available, never blocked, and the failure
screen links straight to it.

Both settings apply to every platform, not just TikTok.

#### Extraction diagnostics

```bash
curl -s localhost:8000/api/health/extraction | jq
```

Reports the installed yt-dlp version and channel, whether impersonation is
actually available (and which targets), ffmpeg/ffprobe presence, and the
cookie file's **shape and age** — format, entry count, which TikTok cookie
*names* are present, and how many days old it is. Never any cookie values,
tokens, or environment secrets.

A cookie export older than 30 days is flagged `likely_stale`: a rejected
session produces exactly the same empty page as a bot-check, so age is worth
knowing before the extractor gets blamed.

## Research mode (topic search → transcript bundle)

The Home page's **Research (transcripts)** tab turns a YouTube topic search
into a structured transcript bundle — for research only, so it **never
downloads video files**, just metadata and captions.

How it works, as a dedicated pipeline (`searching → fetching_captions →
generating_metadata → zipping`) on the same job/queue/SSE machinery:

1. **Search** via yt-dlp's `ytsearch{2N}:` (Top mode) or `ytsearchdate{2N}:`
   (Newest mode) pseudo-URLs — no YouTube Data API, no API key. It
   over-fetches ~2× the requested count, pulls full metadata per candidate,
   applies your filters (min views / uploaded within X days / max duration),
   sorts (Top: by views desc; Newest: by upload date desc), and keeps the
   top N (default 15, max 25).
2. **Captions only** per retained video: manual subtitles are preferred,
   auto-generated captions are the fallback, and a video with neither is
   recorded as skipped ("No captions available") — one bad video never kills
   the job, matching the failure model everywhere else. The same pinned
   yt-dlp + self-update-on-extractor-error + `COOKIES_FILE` infrastructure
   is reused as-is.
3. **Cleaning**: VTT/SRT files are converted to readable plain text —
   timestamps, cue indices, and formatting tags stripped, the duplicated
   rolling lines from auto captions collapsed, and the result merged into
   paragraphs. Optimized for reading and LLM ingestion, not subtitle
   playback. `caption_source` (`manual`/`auto`) is recorded per video so
   downstream consumers know the fidelity.
4. **Bundle**: everything lands in `research-manifest.json` (query, mode,
   filters, per-video metadata, transcript paths, and per-video skip
   reasons) plus `transcripts/{VIDEO_ID}.txt`, zipped and downloadable as
   `research-{query-slug}-{YYYY-MM-DD}.zip`.

The results page lists every video (linked title, channel, views, likes,
date, duration), shows which got transcripts vs. why they were skipped, and
lets you preview any transcript inline.

Note: the database schema migrates automatically on startup (new columns are
added in place via `ALTER TABLE`), so upgrading an existing deployment does
not require deleting the data volume.

## Environment variables

See `.env.example` for the full annotated list. Highlights:

| Variable | Default | Notes |
|---|---|---|
| `MAX_UPLOAD_MB` | 61440 (60GB) | Upload size cap. Rejected with 413 from `Content-Length` before any bytes transfer, and again mid-stream for chunked bodies |
| `MIN_FREE_DISK_MB` | 2048 | Safety margin kept free; uploads are rejected up front (507) if accepting the file would eat into it |
| `UPLOAD_DISK_HEADROOM_MULTIPLIER` | 1.5 | Disk required = upload size x this + the margin, covering extracted frames, temp audio, and the ZIP |
| `UPLOAD_CHUNK_BYTES` | 8388608 (8MiB) | Streaming write size |
| `ZIP_INCLUDE_SOURCE_VIDEO` | false | Whether `output.zip` contains the source video — see [Large uploads](#large-uploads) |
| `STORYBOARD_ENABLED` | true | Build contact sheets after extraction — see [Storyboards](#storyboards) |
| `STORYBOARD_SHEET_WIDTH_PX` | 2400 | Sheet width; tiles are sized from this and the column count |
| `STORYBOARD_MAX_SHEET_HEIGHT_PX` | 4200 | Stops a portrait grid becoming an unreadable ribbon |
| `STORYBOARD_MAX_TILES_PER_SHEET` | 24 | Upper bound per sheet; more tiles means smaller ones |
| `STORYBOARD_COLUMNS_PORTRAIT` / `_LANDSCAPE` | 5 / 6 | Orientation-aware column defaults |
| `STORYBOARD_JPEG_QUALITY` | 90 | Sheet JPEG quality |
| `STORYBOARD_INCLUDE_CAPTIONS` | true | Draw frame number, timestamp and transcript line under each tile |
| `STORYBOARD_THEME` | dark | `dark` or `light` sheet chrome |
| `STORYBOARD_TIMELINE_INTERVAL_SECONDS` | 1.0 | Target spacing for the uniform timeline sheet |
| `STORYBOARD_KEY_MOMENTS_MAX` | 24 | Cap for the key-moments summary (also bounded to one sheet) |
| `WHISPER_MODEL_SIZE` | small | faster-whisper model; example environment uses automatic CUDA with CPU fallback |
| `ENABLE_DIARIZATION` | false | See [Diarization](#optional-speaker-diarization) below |
| `MAX_FRAMES` | 20000 | Global main-frame ceiling; per-job budget defaults to 2,000. Bursts share this budget; opening/event groups are additional and separately bounded. |
| `ADAPTIVE_MIN_FRAMES` / `ADAPTIVE_MAX_FRAMES` | 30 / 2000 | Bounds for adaptive mode's target frame count; default target 300 |
| `RETENTION_HOURS` | 72 | Jobs + artifacts are deleted this many hours after completion by a periodic Celery task |
| `YTDLP_COMMENT_LIMIT` | 100 | Top comments (by likes) saved per URL-ingested job (`MAX_COMMENTS` accepted as an alias) |
| `TIKTOK_DEVICE_ID` | (unset) | Switches TikTok to its mobile API instead of scraping the web page. Get it from the TikTok app: Settings → scroll to the bottom → tap the version number 5×. See [TikTok extraction](#tiktok-extraction) |
| `YTDLP_EXTRACTOR_ARGS` | (unset) | Raw `--extractor-args` passthrough, semicolon-separated (e.g. `tiktok:app_info=…;youtube:player_client=web`) |
| `YTDLP_CHANNEL` | stable | `stable` or `nightly`. Applied at worker startup, never during a job |
| `YTDLP_UPDATE_ON_STARTUP` | false | Install `YTDLP_CHANNEL` when the worker boots. Off by default so a container matches its pinned image |
| `YTDLP_IMPERSONATE_TARGET` | chrome | TLS fingerprint forced on every request, when curl_cffi makes it available |
| `YTDLP_RETRY_DELAY_SECONDS` | 2.0 | Pause before the single anonymous retry |
| `OPENING_DENSE_DURATION` / `OPENING_DENSE_INTERVAL` | 8 / 0.25 | Dense hook-analysis frames: one every INTERVAL seconds for the first DURATION seconds; per-job overridable, disable per job with `opening_dense_enabled=false` |
| `COOKIES_FILE` | (unset) | In-container path to a cookies.txt for account-gated fetches — use `/run/secrets/cookies.txt` and drop the file at `secrets/cookies.txt` on the host; optional |
| `STALE_JOB_TIMEOUT_MINUTES` | 30 | A *running* job with no heartbeat update for this long is marked `failed` (worker crash recovery) |
| `QUEUED_JOB_TIMEOUT_HOURS` | 24 | A job still *waiting* this long fails as `never_started`. Separate from the above so a job queued behind a huge one isn't killed for waiting |
| `TASK_SOFT_TIME_LIMIT_SECONDS` / `TASK_HARD_TIME_LIMIT_SECONDS` | 86400 / 86700 | Whole-job ceiling (24h), sized for very large sources |
| `FFMPEG_TIMEOUT_SECONDS` / `WHISPER_TIMEOUT_SECONDS` | 21600 | Per-step ceilings (6h each) |
| `CORS_ORIGINS` | http://localhost:3000 | Comma-separated list |

### Large uploads

The default cap is **60GB**. Two things make that workable:

**Files stream straight to disk.** `POST /api/jobs/upload` takes the raw file as
the request body with options in the query string, and writes it to `/data` in
8MiB chunks while hashing as it goes — nothing is buffered. The multipart route
(`POST /api/jobs`) still exists for URL jobs and small files, but Starlette
buffers each multipart part to a temp file *before* the handler runs, so a
60GB multipart upload would need a second full copy on the container's own
filesystem — which the disk guard cannot even see. The frontend automatically
uses the streaming route for file uploads.

Oversized uploads are rejected in **413 before a single byte transfers** when the
client sends a `Content-Length` (browsers do), so nobody waits out a 60GB
transfer to learn the file was too big.

**`output.zip` excludes the source video.** The archive carries the analysis —
transcripts, frames, metadata, manifest — while the video itself stays on disk
and is downloadable from `/api/jobs/{id}/video` (which supports Range
requests). Zipping the source would deflate tens of GB of already-compressed
H.264 for ~0% saving, costing hours of CPU and a second full copy on the same
volume. The manifest still describes the source file and marks it
`"included_in_zip": false` with the reason, and `metadata/zip_exclusions.json`
records it. Set `ZIP_INCLUDE_SOURCE_VIDEO=true` to restore the old behavior.

**Disk sizing is the real constraint.** Budget roughly **1.5x the video size**
(`UPLOAD_DISK_HEADROOM_MULTIPLIER`) plus `MIN_FREE_DISK_MB`: a 60GB upload needs
~92GB free. On Docker Desktop that means the VM's virtual disk, not just the
host drive — raise it in Settings → Resources if needed. The guard rejects with
a 507 explaining the arithmetic rather than failing partway with `ENOSPC`.

**Processing time.** A 60GB source is many hours of CPU at
`WHISPER_MODEL_SIZE=small` with `TRANSCRIPTION_CONCURRENCY=1`, so the job
ceiling is 24h and scene detection, transcription, and zipping all send
heartbeats to keep the stale-job reaper from failing healthy work. Uploads have
no resume: a dropped connection means starting over.

### Optional speaker diarization

`ENABLE_DIARIZATION=true` turns on a WhisperX-based diarization pass that
adds a `speaker` field to each transcript segment. It's off by default because
it needs extra heavyweight setup not installed by default:

1. Install `whisperx` and `torch` in the worker image (`pip install whisperx`;
   this pulls in `pyannote.audio` and a real PyTorch build).
2. Accept the pyannote model terms on HuggingFace and set `HF_TOKEN` to an
   access token with read access.
3. Rebuild the worker image and set `ENABLE_DIARIZATION=true` in `.env`.

If the flag is on but `whisperx` isn't installed, the step logs a warning and
falls back to a transcript without speaker labels rather than failing the job.

## Extending the pipeline

Processing is an ordered list of `PipelineStep` subclasses
(`backend/app/pipeline/base.py`), each declaring what it `consumes`/`produces`
from a shared context and reporting its own progress:

```python
class MyStep(PipelineStep):
    name = "my_step"          # must be added to PIPELINE_STEPS in app/models.py
    label = "My step"
    consumes = ("frames",)
    produces = ("my_analysis",)

    def run(self, ctx: PipelineContext) -> None:
        frames = ctx.shared["frames"]
        ctx.set_step_progress(self.name, 0)
        # ... do work, call ctx.storage.save_bytes(...) for outputs ...
        ctx.shared["my_analysis"] = {...}
        ctx.set_step_progress(self.name, 100)
```

To wire it in:

1. Add `"my_step"` to `PIPELINE_STEPS` in `backend/app/models.py` (controls
   valid job status values and overall-progress weighting).
2. Add an instance to the list built in `_build_pipeline()` in
   `backend/app/pipeline/runner.py`, in the position it belongs.
3. If it should show up in `manifest.json`, write its results under
   `manifest["analyses"][...]` in `GenerateMetadataStep` (or append your own
   step after it that does so) — that key is reserved exactly for this.
4. Add the step name/label to `PIPELINE_STEP_ORDER` in
   `frontend/lib/types.ts` so the Processing view's step indicator shows it.

Version 0.2 implements this extension path for local OCR and visual observations.
Object detection, embeddings and semantic search can use the same pipeline;
new user-facing controls or output views may also require API and frontend changes.

## Regenerating frontend types from the API

`frontend/lib/types.ts` is hand-maintained to mirror
`backend/app/schemas.py` so the frontend builds without the API running. To
regenerate a fully derived version once the API is up:

```bash
cd frontend
API_BASE_URL=http://localhost:8000 npm run generate-types
```

This writes `frontend/lib/openapi-types.ts` from the live OpenAPI schema.

## Tests

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

- Unit tests mock ffmpeg/faster-whisper and cover each pipeline step in
  isolation, the "no audio track → skip transcription" path, and the
  `every_frame` over-cap rejection.
- `tests/test_integration_pipeline.py` runs the *real* pipeline (real ffmpeg,
  real PySceneDetect, real faster-whisper `tiny` model) against a synthetic
  5-second sample video generated by `backend/scripts/generate_sample.py`. It
  skips automatically if `ffmpeg` isn't on `PATH` or the model can't be
  downloaded (no network) — those are environment constraints, not test
  failures.

## Non-goals (v1)

No auth/accounts, no cloud storage, no GPU requirement, no video editing, no
multi-tenancy, no billing.
