# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# OCR + scanned-PDF support — pdfplumber doesn't pull these.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv — 10× faster than pip on cold installs (parallel HTTP/2 fetches)
# and reuses a BuildKit cache across rebuilds. The official wheel is small
# and self-contained.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore uv

COPY requirements.txt /app/
# `--system` installs into the global site-packages (matches old pip layout).
# `--mount=type=cache` lets uv reuse already-downloaded wheels across builds —
# the next change to requirements.txt only re-fetches what actually changed.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-progress -r requirements.txt

# `/fill-form` and `/to-acroform` call these repo-root modules at runtime.
COPY run_pipeline.py field_detector.py field_normalizer.py \
     flatlist_adapter.py questionnaire_adapter.py form_filler.py \
     form_utils.py docx_detector.py docx_filler.py acroform_writer.py \
     /app/

COPY api/ /app/api/

ENV PYTHONUNBUFFERED=1 \
    OLLAMA_URL=http://host.docker.internal:11434 \
    REDIS_HOST=redis \
    JOBS_DIR=/var/lib/form-pipeline/jobs

RUN useradd --system --uid 1000 app \
    && mkdir -p /var/lib/form-pipeline/jobs \
    && chown -R app:app /var/lib/form-pipeline /app
USER app

EXPOSE 8000

# Default command runs the API; the worker service overrides this.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
