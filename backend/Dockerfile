FROM python:3.11-slim

# ffmpeg for probing/transcoding/frame extraction; libgl1 for opencv (used by
# PySceneDetect's content detector); curl for the Docker healthcheck;
# fonts-dejavu-core for storyboard captions — python:3.11-slim ships no fonts
# at all, and --no-install-recommends stops ffmpeg from pulling any in, so
# without this Pillow falls back to a tiny unreadable bitmap font.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional CUDA runtime for this machine's NVIDIA worker. The base image remains
# usable on CPU hosts; enable via ENABLE_CUDA=true at build time.
ARG ENABLE_CUDA=false
RUN if [ "$ENABLE_CUDA" = "true" ]; then \
      pip install --no-cache-dir nvidia-cublas-cu12==12.8.4.1 nvidia-cudnn-cu12==9.10.2.21; \
    fi
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

# Optional: bake the Whisper weights into the image so the container never
# needs to download them at runtime (useful for offline/air-gapped hosts).
# Off by default — the `whisper_cache` volume in docker-compose.yml already
# makes the download a one-time cost, and baking adds ~460MB to the image.
#   docker compose build --build-arg PREFETCH_WHISPER_MODEL=true worker
ARG PREFETCH_WHISPER_MODEL=false
ARG WHISPER_MODEL_SIZE=small
ENV HF_HOME=/root/.cache/huggingface
RUN if [ "$PREFETCH_WHISPER_MODEL" = "true" ]; then \
      python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL_SIZE}', device='cpu', compute_type='int8')" ; \
    fi

COPY . .

RUN mkdir -p /data/jobs /data/db

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
