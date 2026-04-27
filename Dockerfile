FROM python:3.12-slim

# OCR + scanned-PDF support — pdfplumber doesn't pull these.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# `/fill-form` calls run_pipeline + its stage modules at the repo root.
COPY run_pipeline.py field_detector.py field_normalizer.py \
     flatlist_adapter.py questionnaire_adapter.py form_filler.py \
     form_utils.py docx_detector.py docx_filler.py \
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
