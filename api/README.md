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

## Quickstart (Docker — the recommended path)

Two `make` targets. Everything else (image build, model pull, tests) is
inside Docker.

```bash
make docker-setup       # one-time: verify Docker + Ollama, pull qwen3:8b, build image
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

### Why Ollama stays on the host

Docker on macOS cannot use the Apple-Metal GPU; running Qwen3:8b on CPU
inside a container is roughly 10× slower. So the containers reach the host
Ollama via `host.docker.internal:11434`. `make docker-setup` checks this
for you and prints a fix-it message if Ollama is bound only to `127.0.0.1`.

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
