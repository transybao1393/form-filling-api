# `api/` — async `data.json` generator

FastAPI service that turns an uploaded questionnaire (PDF / scanned PDF /
image / DOCX) plus optional reference documents into a `data.json` file
matching the schema consumed by `run_pipeline.py`. Generation runs on the
host-native [`llm_service`](../llm_service/README.md), which wraps a local
**Qwen3:8b** model on Ollama (thinking mode disabled). HTTP submissions
return immediately; an **arq** worker pulls the job and POSTs to the LLM
service over `host.docker.internal:11500`.

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

## Quickstart (Docker — the recommended path)

Three `make` targets. The first starts the host-native LLM service (which
needs Ollama already running on the host); the other two manage the
dockerized API stack.

```bash
make ollama-service-up  # host-native: verifies Ollama, pulls qwen3:8b, starts llm_service on :11500
make docker-setup       # one-time: verify Docker + build the api image
make docker-up          # start redis + api + worker, then run the test suite
```

When `docker-up` finishes you'll see test output and links to:

- Scalar API reference: <http://localhost:8000/scalar>
- Swagger UI: <http://localhost:8000/docs>
- Health: <http://localhost:8000/healthz>

Other helpers:

```bash
make docker-test        # re-run the test suite without restarting
make docker-logs        # tail api + worker logs
make docker-down        # stop the stack (preserves the jobs volume)
```

### How the LLM call works (three-tier)

The API container does **not** talk to Ollama directly. All LLM-orchestration
concerns (prompt building, `/api/chat`, JSON parsing + one-shot repair,
output normalization) live in a separate **host-native** FastAPI process —
the **llm_service** — started by `make ollama-service-up`. The worker POSTs
extracted questionnaire/reference text to `http://host.docker.internal:11500`
and gets back a validated `data.json`.

```
Ollama (:11434, native, Metal GPU)
        ▲ /api/chat
llm_service (:11500, native, own venv)
        ▲ POST /generate
        └── host.docker.internal:11500 ── api + worker (docker-compose)
```

The split exists because Docker on macOS cannot reach the Apple Metal GPU
(CPU-only Qwen3:8b is ~10× slower). Keeping the LLM service on the host
gives us GPU access **and** a clean service boundary: the main app can be
rebuilt, restarted, and tested without touching the LLM weights.

See `llm_service/README.md` for the service's endpoints + env vars.

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
  "stage": "calling_llm_service",
  "stage_text": "Generating answers",
  "submitted_at": "2026-04-27T10:12:00+00:00",
  "started_at": "2026-04-27T10:12:01+00:00",
  "completed_at": null,
  "error": null,
  "download_url": null
}
```

`status` is one of: `queued` → `running` → `completed` | `failed`.

Stage progression:
| %   | stage                       | stage_text |
|-----|-----------------------------|------------|
| 0   | `queued`                    | Queued, waiting for worker |
| 10  | `extracting_questionnaire`  | Reading the questionnaire |
| 25  | `extracting_references`     | Reading reference documents |
| 40  | `calling_llm_service`       | Generating answers |
| 99  | `saving`                    | Writing data.json |
| 100 | `completed` / `review`      | Done / awaiting human review |

All prompt building, parsing, normalization, and renumbering happens
inside the host-native `llm_service`; the worker just waits for the response.

### `GET /jobs/{job_id}/data.json` — download result

Returns 200 with `Content-Disposition: attachment; filename="data.json"`
once `status="completed"`. Returns 409 + the current state body otherwise.

### `POST /to-acroform` — convert a PDF to editable AcroForm (sync)

Returns an AcroForm PDF where every detected field is a real, editable widget.
Reviewers can fix wrong values inline in any PDF viewer instead of bouncing
back through the API.

| Field | Required | Notes |
|---|---|---|
| `form_file` | yes | **PDF only** (DOCX is rejected with 415) |
| `data_file` | no | If supplied, widgets are pre-populated from this `data.json` |
| `answers_file` | no | Optional flat `{question_id: answer}` for nested data |
| `format` | no | `flat | flatlist | nested` override |

Response headers tell you which path ran and how the values were sourced:

| Header | Meaning |
|---|---|
| `X-Acroform-Source: existing` | Input already had AcroForm widgets — fast-path |
| `X-Acroform-Source: injected` | Input was a non-AcroForm PDF — detection + injection ran |
| `X-Fields-Total` | Total widgets in the resulting AcroForm |
| `X-Fields-Filled` | Widgets pre-populated from `data_file` |
| `X-Fields-Carried-Over` | Widgets pre-populated from text already drawn on the page (recovered from a previously overlay-filled PDF) |

**Carry-over rule** — when the input is a non-AcroForm PDF that already has
text drawn inside the detected field bboxes, that text is recovered into each
widget's default value. Per-field precedence: `data_file` > carried-over text
> empty.

> **Recommended flow for editable output:** call `/to-acroform` with the same
> `form_file + data_file` you'd pass to `/fill-form`. That produces an
> editable AcroForm pre-populated with values in one step.
>
> **Carry-over is best-effort** — it works fully when the input PDF already
> uses native AcroForm widgets (Acrobat-style round-trip) or when fields are
> table cells. It is **partial for `/fill-form`-overlay output**: the
> detector identifies fields by visual emptiness (underscore runs, "Label:"
> blank space, bordered rectangles), and `/fill-form` overlays text onto
> those gaps — once filled, those gaps no longer look empty, so the detector
> rediscovers fewer fields. Avoid the round-trip when possible; pass data
> directly to `/to-acroform`.

```bash
# 1. blank PDF + data → editable AcroForm with values
curl -F "form_file=@input/test6/questionnaire_blank.pdf" \
     -F "data_file=@input/test6/data.json" \
     http://localhost:8000/to-acroform -o filled-acroform.pdf

# 2. overlay-filled PDF (no data) → editable AcroForm with carry-over values
curl -F "form_file=@some-overlay-filled.pdf" \
     http://localhost:8000/to-acroform -o recovered-acroform.pdf
```

### `GET /healthz`

```json
{
  "status": "ok",
  "llm_service": "ok",
  "redis": "ok",
  "model": "qwen3:8b"
}
```

`llm_service` is `"ok"` iff the host-native LLM service responds AND its
upstream Ollama is reachable. `model` mirrors what that service reports
(empty string if the service is down). Status code is always 200 — read
the body to decide what to page on.

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
| `LLM_SERVICE_URL`            | `http://localhost:11500` (Docker: `http://host.docker.internal:11500`) | Where the host-native llm_service is reachable |
| `LLM_SERVICE_TIMEOUT`        | `360`                              | Per-call HTTP timeout when POSTing to llm_service `/generate` |
| `REDIS_HOST`                 | `localhost` (Docker: `redis`)      | Broker host |
| `REDIS_PORT`                 | `6379`                             | Broker port |
| `JOBS_DIR`                   | `~/.form_pipeline/jobs` (Docker: `/var/lib/form-pipeline/jobs`) | Where uploads + state + result live |
| `JOB_TTL_HOURS`              | `168` (7 days)                     | When the worker GC removes finished job dirs |

> The Ollama-specific knobs (`OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT`,
> `OLLAMA_NUM_CTX`, `OLLAMA_TEMPERATURE`, `MAX_CHARS_PER_DOC`,
> `MAX_CHARS_PER_QUESTIONNAIRE`) now live in `llm_service/README.md` — they
> only affect the host-native LLM service, not this API.
