# `api/` — async `data.json` generator

FastAPI service that turns an uploaded questionnaire (PDF / scanned PDF /
image / DOCX) plus optional reference documents into a `data.json` file
matching the schema consumed by `run_pipeline.py`. Generation is done by
a local **Qwen3:8b** model on Ollama (thinking mode disabled), driven by
an **arq** worker so the HTTP submission returns immediately.

```
                   ┌──────────────┐ enqueue ┌─────────┐  pull  ┌────────────┐
client ──POST──►   │  FastAPI api │────────►│  Redis  │◄───────│  arq worker│
                   └──────┬───────┘         └─────────┘        └─────┬──────┘
                          │ writes uploads/state.json                │ writes result.json
                          ▼                                          ▼
                     JOBS_DIR/<id>/                              JOBS_DIR/<id>/
                                                                 result.json
client ──GET /jobs/{id}─────────► (reads state.json)
client ──GET /jobs/{id}/data.json (reads result.json)
```

## Prerequisites (host install)

```bash
# macOS
brew install tesseract poppler redis
brew services start redis

# Debian / Ubuntu
sudo apt-get install tesseract-ocr poppler-utils redis-server
sudo systemctl enable --now redis
```

Python deps (from repo root):
```bash
make setup
.venv/bin/pip install -r requirements.txt    # picks up arq
```

Ollama + model:
```bash
ollama pull qwen3:8b
ollama serve   # in a separate terminal
```

## Run (host, two processes)

```bash
# Terminal 1 — API
.venv/bin/uvicorn api.main:app --reload --port 8000

# Terminal 2 — worker
.venv/bin/arq api.worker.WorkerSettings
```

Swagger UI: <http://localhost:8000/docs>

## Run (Docker, recommended for the deployment)

The repo ships a `Dockerfile` and `docker-compose.yml` that bring up Redis,
the API, and the worker in a single command. Ollama stays on the host
because Docker on macOS cannot use the Metal GPU; the containers reach it
via `host.docker.internal:11434`.

```bash
# from repo root
docker compose build
docker compose up -d
docker compose logs -f api worker
```

If `GET /healthz` returns `ollama=down`, your host Ollama is bound only to
`127.0.0.1`. Make it listen on all interfaces:

```bash
# macOS — set the launchctl env var and restart Ollama
launchctl setenv OLLAMA_HOST 0.0.0.0:11434
killall Ollama && open -a Ollama
```

## Endpoints

### `POST /generate-data-json` — submit (async)

Multipart form. Returns **202 Accepted** within ~100 ms.

| Field | Required | Notes |
|---|---|---|
| `questionnaire_file` | yes | PDF / DOCX / PNG / JPEG / scanned PDF |
| `reference_files` | no | Repeat the field for multiple files |
| `questionnaire_title` | no | Title override |

Response:
```json
{
  "job_id": "8e1d…",
  "status": "queued",
  "status_url": "/jobs/8e1d…",
  "download_url": "/jobs/8e1d…/data.json"
}
```

### `GET /jobs/{job_id}` — status & progress

```json
{
  "job_id": "8e1d…",
  "status": "running",
  "percent": 40,
  "stage": "calling_llm",
  "stage_text": "Generating answers (Qwen3:8b)",
  "submitted_at": "2026-04-27T10:12:00+00:00",
  "started_at": "2026-04-27T10:12:01+00:00",
  "completed_at": null,
  "error": null,
  "download_url": null
}
```

`status` is one of: `queued` → `running` → `completed` | `failed`.

Stage progression:
| % | stage | stage_text |
|---|---|---|
| 0  | `queued`                  | Queued, waiting for worker |
| 10 | `extracting_questionnaire`| Reading the questionnaire |
| 25 | `extracting_references`   | Reading reference documents |
| 35 | `building_prompt`         | Building prompt for Qwen3:8b |
| 40 | `calling_llm`             | Generating answers (Qwen3:8b) |
| 90 | `normalizing`             | Validating and normalizing output |
| 99 | `saving`                  | Writing data.json |
| 100| `completed`               | Done |

### `GET /jobs/{job_id}/data.json` — download result

Returns 200 with `Content-Disposition: attachment; filename="data.json"`
once `status="completed"`. Returns 409 + the current state body otherwise.

### `GET /healthz`

```json
{ "ollama": "ok", "model": "qwen3:8b" }
```

## End-to-end example

```bash
JOB=$(curl -s -F "questionnaire_file=@input/test3/blank_questionnaire.pdf" \
            -F "reference_files=@/path/to/source.pdf" \
            http://localhost:8000/generate-data-json | jq -r .job_id)

# poll
while true; do
  STATE=$(curl -s http://localhost:8000/jobs/$JOB)
  echo "$STATE" | jq -c '{status, percent, stage, stage_text}'
  test "$(echo "$STATE" | jq -r .status)" = "completed" && break
  sleep 1
done

# download
curl -OJ http://localhost:8000/jobs/$JOB/data.json   # writes ./data.json

# round-trip into the existing pipeline
python3 run_pipeline.py input/test3/blank_questionnaire.pdf data.json \
        --workdir output/test3 -o output/test3/filled.pdf
```

## Configuration (env vars)

| Variable                     | Default                            | Purpose |
|------------------------------|------------------------------------|---------|
| `OLLAMA_URL`                 | `http://localhost:11434`           | Ollama daemon URL (in Docker: `host.docker.internal:…`) |
| `OLLAMA_MODEL`               | `qwen3:8b`                         | Model tag |
| `OLLAMA_TIMEOUT`             | `300`                              | Per-call HTTP timeout (s) |
| `OLLAMA_NUM_CTX`             | `16384`                            | Context window — bumping past 24 k pays a 4-min KV alloc tax on Apple Silicon |
| `OLLAMA_TEMPERATURE`         | `0.1`                              | Sampling temperature |
| `MAX_CHARS_PER_DOC`          | `8000`                             | Per-reference truncation cap |
| `MAX_CHARS_PER_QUESTIONNAIRE`| `25000`                            | Questionnaire truncation cap |
| `REDIS_HOST`                 | `localhost` (Docker: `redis`)      | Broker host |
| `REDIS_PORT`                 | `6379`                             | Broker port |
| `JOBS_DIR`                   | `~/.form_pipeline/jobs` (Docker: `/var/lib/form-pipeline/jobs`) | Where uploads + state + result live |
| `JOB_TTL_HOURS`              | `168` (7 days)                     | When the worker GC removes finished job dirs |
