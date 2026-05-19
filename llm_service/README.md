# llm_service

Host-native FastAPI process that owns every Ollama-related concern for the
form-pipeline backend. The main app (running in docker-compose) treats this
service as an opaque HTTP dependency: it POSTs questionnaire + reference text
and gets back a validated `data.json`.

This service runs **natively on the host** — not in a container — so the
underlying Ollama daemon can use Apple Metal GPU on macOS (Docker on macOS
can't reach the GPU; CPU-only Qwen3:8b is ~10× slower).

## Architecture

```
Ollama (native, :11434)  ←  llm_service (native, :11500)  ←  api/worker (docker)
```

## Endpoints

| Method | Path           | Purpose                                                                    |
|--------|----------------|----------------------------------------------------------------------------|
| POST   | `/generate`    | Run the full LLM pipeline. Body: `{questionnaire_text, references, ...}`. Returns `{data: <data.json>}`. |
| GET    | `/healthz`     | Ping Ollama, return `{status, ollama, model}`.                             |
| POST   | `/pull-model`  | Stream `ollama pull <model>` output. Lets a fresh box warm itself.         |

See `schemas.py` for full request/response shapes.

## Run

From the repo root:

```bash
make ollama-service-up      # starts on http://localhost:11500
make ollama-service-logs    # tail the log
make ollama-service-down    # stop
make ollama-service-clean   # nuke the venv (forces fresh deps next start)
```

### Self-bootstrap

On startup (`bootstrap.ensure_ready()`) the service:

1. **Fails fast if `ollama` isn't installed.** Refuses to come up; install
   Ollama from <https://ollama.com> and try again.
2. **Soft-warns if the Ollama daemon isn't reachable.** Service still
   starts; `/healthz` will report `ollama: down` until the daemon comes
   up. Start it with the Ollama app or `ollama serve`.
3. **Pulls `$OLLAMA_MODEL` if missing.** First boot streams the ~5 GB
   download to the service log; subsequent boots are instant because the
   model lives in `~/.ollama/models`.

This means `make ollama-service-up` is a single idempotent command — no
external pre-flight. The Makefile target just launches uvicorn and waits
for `/healthz`.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_URL` | `http://localhost:11434` | Where the host Ollama daemon is listening. |
| `OLLAMA_MODEL` | `qwen3:8b` | Model tag passed to `/api/chat`. |
| `OLLAMA_TIMEOUT` | `300` | Per-request HTTP timeout (seconds). |
| `OLLAMA_NUM_CTX` | `16384` | Context window. Don't bump past 16k on Apple Silicon — KV-cache allocation gets painful. |
| `OLLAMA_TEMPERATURE` | `0.1` | Sampling temperature. Low for deterministic structured output. |
| `MAX_CHARS_PER_DOC` | `8000` | Per-reference character cap before truncation. |
| `MAX_CHARS_PER_QUESTIONNAIRE` | `25000` | Questionnaire character cap (looser — truncating loses whole items). |
| `LLM_SERVICE_HOST` | `0.0.0.0` | uvicorn bind host. Keep `0.0.0.0` so dockerized callers can reach it via `host.docker.internal`. |
| `LLM_SERVICE_PORT` | `11500` | uvicorn bind port. |

## Local install (without Make)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r llm_service/requirements.txt
uvicorn llm_service.main:app --host 0.0.0.0 --port 11500
```
