# Form Field Detection & Filling Pipeline
#
# === Full-stack quick start (host Ollama + host llm_service + dockerized API) ===
#   1. make ollama-service-up    Verify host Ollama, pull qwen3:8b, start the
#                                host-native llm_service in its own venv.
#   2. make docker-setup         One-time Docker checks + build the api image.
#   3. make docker-up            Start redis + api + worker, run tests, ready.
#
# === Docker workflow (recommended — runs the API stack and tests inside containers) ===
#   make docker-setup     One-time setup: verify Docker, build image.
#   make docker-up        Start redis + api + worker, then run the test suite to verify.
#   make docker-down      Stop the stack (preserves the jobs volume).
#   make docker-logs      Tail api + worker logs.
#   make docker-test      Re-run the test suite without restarting.
#
# === LLM orchestration service (host-native, runs in its own venv) ============
#   make ollama-service-up    Start (creates venv on first run; idempotent).
#   make ollama-service-down  Stop.
#   make ollama-service-logs  Tail llm_service log.
#
# === Host CLI workflow (legacy — runs run_pipeline.py via .venv) ===
#   make setup
#   make run NAME=<name>          # single command — auto-detects format
#   make list                     # list all available NAMEs
# Folder convention for the CLI: input/<name>/{*.pdf, data.json, answers.json?} → output/<name>/

# ---- Config ----------------------------------------------------------------

NAME      ?= ics
INPUT_DIR := input/$(NAME)
OUT_DIR   := output/$(NAME)

# Auto-discover files in input/<name>/ (filename doesn't matter)
PDF       ?= $(firstword $(wildcard $(INPUT_DIR)/*.pdf))
DATA      ?= $(firstword $(wildcard $(INPUT_DIR)/data.json $(INPUT_DIR)/*.json))
ANSWERS   ?= $(wildcard $(INPUT_DIR)/answers.json)

# Output paths (stable, derived from NAME)
FIELDS_RAW    := $(OUT_DIR)/fields.json
FIELDS        := $(OUT_DIR)/fields_normalized.json
FIELDS_ENRICH := $(OUT_DIR)/fields_enriched.json
TEMPLATE      := $(OUT_DIR)/template.json
OUT           ?= $(OUT_DIR)/filled.pdf
PREVIEW       := $(OUT_DIR)/filled_preview.png
DEBUG         := $(OUT_DIR)/debug.png

VENV      ?= .venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip

.PHONY: help setup check-name check-pdf check-data ensure-out-dir \
        run detect normalize fill \
        visualize template preview \
        list clean clean-all distclean \
        docker-setup docker-up docker-down docker-logs docker-test \
        ollama-service-up ollama-service-down ollama-service-logs ollama-service-clean

# ---- Sanity checks ---------------------------------------------------------

check-name:
	@if [ -z "$(NAME)" ]; then \
	  echo "ERROR: NAME is required (e.g. make run NAME=test1)"; exit 1; fi

check-pdf: check-name
	@if [ -z "$(PDF)" ] || [ ! -f "$(PDF)" ]; then \
	  echo "ERROR: No PDF found in $(INPUT_DIR)/"; \
	  echo "       Place a *.pdf file there, or pass PDF=path/to/file.pdf"; \
	  exit 1; fi

check-data: check-name
	@if [ -z "$(DATA)" ] || [ ! -f "$(DATA)" ]; then \
	  echo "ERROR: No data JSON found in $(INPUT_DIR)/"; \
	  echo "       Place a data.json (or any *.json) there,"; \
	  echo "       or pass DATA=path/to/file.json"; \
	  exit 1; fi

ensure-out-dir: check-name
	@mkdir -p $(OUT_DIR)

# ---- Help ------------------------------------------------------------------

help:
	@echo "Form Field Detection & Filling Pipeline"
	@echo ""
	@echo "Folder convention:"
	@echo "  input/<name>/   form.pdf (any filename), data.json, answers.json"
	@echo "  output/<name>/  all generated files"
	@echo ""
	@echo "Main targets:"
	@echo "  make setup          Install deps into .venv/"
	@echo "  make run NAME=<n>   Run pipeline (auto-detects data format)"
	@echo "  make list           List available NAMEs in input/"
	@echo ""
	@echo "Individual stages (rarely needed — 'make run' handles everything):"
	@echo "  make detect NAME=<n>     Stage 1 only"
	@echo "  make normalize NAME=<n>  Stage 2 only"
	@echo "  make fill NAME=<n>       Stage 3 only (assumes stages 1+2 done)"
	@echo ""
	@echo "Debug helpers:"
	@echo "  make visualize NAME=<n>  Draw detected bboxes on PDF"
	@echo "  make template NAME=<n>   Generate empty user_data scaffold"
	@echo "  make preview NAME=<n>    Render filled PDF to PNG"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean NAME=<n>      Delete output/<n>/"
	@echo "  make clean-all           Delete all of output/"
	@echo "  make distclean           clean-all + remove venv"
	@echo ""
	@echo "Current config:"
	@echo "  NAME      = $(NAME)"
	@echo "  PDF       = $(PDF)"
	@echo "  DATA      = $(DATA)"
	@echo "  ANSWERS   = $(ANSWERS)"
	@echo "  OUT       = $(OUT)"

list:
	@if [ -d input ]; then \
	  echo "Available NAMEs in input/:"; \
	  for d in input/*/; do \
	    name=$$(basename "$$d"); \
	    pdf=$$(ls "$$d"*.pdf 2>/dev/null | head -1); \
	    data=$$(ls "$$d"data.json "$$d"*.json 2>/dev/null | head -1); \
	    answers=$$(ls "$$d"answers.json 2>/dev/null); \
	    printf "  %-20s  pdf=%s  data=%s  answers=%s\n" "$$name" \
	      "$$([ -n "$$pdf" ] && echo yes || echo NO)" \
	      "$$([ -n "$$data" ] && echo yes || echo NO)" \
	      "$$([ -n "$$answers" ] && echo yes || echo no)"; \
	  done; \
	else \
	  echo "No input/ folder found. Create input/<name>/ with a PDF."; \
	fi

# ---- Setup -----------------------------------------------------------------

setup: $(VENV)/.installed

$(VENV)/.installed: requirements.txt
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)/.installed
	@echo "Setup complete."

# ---- Main ------------------------------------------------------------------

# Single command — auto-detects data format (flat / flatlist / nested)
# and dispatches to the right adapter.
run: setup check-pdf check-data ensure-out-dir
	@echo "[$(NAME)] Running pipeline on $(PDF)"
	$(PY) run_pipeline.py $(PDF) $(DATA) \
	      --workdir $(OUT_DIR) \
	      -o $(OUT) \
	      $(if $(ANSWERS),--answers $(ANSWERS),)
	@echo "[$(NAME)] Done."

# ---- Individual stages (for debugging / advanced use) ----------------------

detect: setup check-pdf ensure-out-dir
	$(PY) field_detector.py $(PDF) -o $(FIELDS_RAW)

normalize: setup ensure-out-dir $(FIELDS_RAW)
	$(PY) field_normalizer.py $(FIELDS_RAW) -o $(FIELDS)

fill: setup check-pdf check-data ensure-out-dir $(FIELDS)
	$(PY) form_filler.py $(PDF) $(FIELDS) $(DATA) -o $(OUT)

# ---- Debug helpers ---------------------------------------------------------

visualize: setup check-pdf ensure-out-dir $(FIELDS_RAW)
	$(PY) form_utils.py visualize $(PDF) $(FIELDS_RAW) -o $(DEBUG)
	@echo "[$(NAME)] -> $(DEBUG)"

template: setup ensure-out-dir $(FIELDS)
	$(PY) form_utils.py template $(FIELDS) -o $(TEMPLATE)
	@echo "[$(NAME)] Template at $(TEMPLATE)"

preview: $(OUT)
	$(PY) -c "from pdf2image import convert_from_path; \
	          convert_from_path('$(OUT)', dpi=150)[0].save('$(PREVIEW)','PNG'); \
	          print('[$(NAME)] -> $(PREVIEW)')"

# ---- Cleanup ---------------------------------------------------------------

clean: check-name
	rm -rf $(OUT_DIR)

clean-all:
	rm -rf output/

distclean: clean-all
	rm -rf $(VENV)

# ---- Docker workflow (the API + arq worker + redis stack) ------------------
#
# Two main targets:
#   make docker-setup    one-time: verify Docker + Ollama, pull model, build image
#   make docker-up       start the stack and run tests inside the api container
#
# Plus convenience: docker-down, docker-logs, docker-test.

docker-setup:
	@command -v docker >/dev/null 2>&1 \
	  || { echo "ERROR: install Docker first (https://docs.docker.com/get-docker/)."; exit 1; }
	@docker compose version >/dev/null 2>&1 \
	  || { echo "ERROR: docker compose plugin not found."; exit 1; }
	docker compose build
	@echo ""
	@echo "Setup complete. Next:"
	@echo "  1) make ollama-service-up   (start the host-native LLM service)"
	@echo "  2) make docker-up           (start redis + api + worker)"

docker-up:
	docker compose up -d
	@printf "Waiting for API /healthz "
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  curl -sf http://localhost:8000/healthz >/dev/null 2>&1 && { echo " ok"; break; }; \
	  printf "."; sleep 2; \
	done
	@printf "Waiting for WS /healthz "
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  curl -sf http://localhost:8001/healthz >/dev/null 2>&1 && { echo " ok"; break; }; \
	  printf "."; sleep 2; \
	done
	@$(MAKE) docker-test
	@echo ""
	@echo "Stack ready:"
	@echo "  Scalar API reference : http://localhost:8000/scalar"
	@echo "  Swagger UI           : http://localhost:8000/docs"
	@echo "  API health           : http://localhost:8000/healthz"
	@echo "  WS health            : http://localhost:8001/healthz"

docker-test:
	docker compose run --rm \
	  -v "$(CURDIR)/tests:/app/tests:ro" \
	  -v "$(CURDIR)/input:/app/input:ro" \
	  -e API_BASE_URL=http://api:8000 \
	  --no-deps \
	  api pytest tests/ -v --tb=short

# Rate-limit tests need the api container to run with TIGHT per-IP limits
# (otherwise a normal test pass would never trip the throttle). This target
# recreates the api container with 3/minute on every limited endpoint, runs
# only the rate-limit suite, and restores the defaults afterwards.
docker-test-rate-limit:
	RATE_LIMIT_GENERATE=3/minute \
	RATE_LIMIT_FILL_FORM=3/minute \
	RATE_LIMIT_TO_ACROFORM=3/minute \
	  docker compose up -d --force-recreate api
	@printf "Waiting for /healthz "
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  curl -sf http://localhost:8000/healthz >/dev/null 2>&1 && { echo " ok"; break; }; \
	  printf "."; sleep 2; \
	done
	# Wipe slowapi's per-IP counters (DB 1) from any prior run so the test
	# starts from a known-clean bucket. arq's queue lives on DB 0 — untouched.
	docker compose exec -T redis redis-cli -n 1 FLUSHDB
	-docker compose run --rm \
	  -v "$(CURDIR)/tests:/app/tests:ro" \
	  -v "$(CURDIR)/input:/app/input:ro" \
	  -e API_BASE_URL=http://api:8000 \
	  -e RATE_LIMIT_GENERATE=3/minute \
	  -e RATE_LIMIT_FILL_FORM=3/minute \
	  -e RATE_LIMIT_TO_ACROFORM=3/minute \
	  --no-deps \
	  api pytest tests/test_rate_limit.py -v --tb=short
	docker compose up -d --force-recreate api

docker-logs:
	docker compose logs -f api worker

docker-down:
	docker compose down

# ---- LLM orchestration service (host-native, isolated venv) ----------------
#
# Runs natively on the host so the upstream Ollama daemon can use the Mac's
# Metal GPU (Docker on macOS can't reach it; CPU-only Qwen3:8b is ~10× slower).
# Lives in its own venv so its slim dependency set stays decoupled from the
# main app's requirements.txt.

LLM_SERVICE_PORT  ?= 11500
LLM_SERVICE_VENV  := llm_service/.venv
LLM_SERVICE_PY    := $(LLM_SERVICE_VENV)/bin/python
LLM_SERVICE_PIP   := $(LLM_SERVICE_VENV)/bin/pip
LLM_SERVICE_UVI   := $(LLM_SERVICE_VENV)/bin/uvicorn
LLM_SERVICE_PID   := .llm_service.pid
LLM_SERVICE_LOG   := .llm_service.log

$(LLM_SERVICE_VENV)/.installed: llm_service/requirements.txt
	@echo "Creating llm_service venv at $(LLM_SERVICE_VENV)/ ..."
	@test -d $(LLM_SERVICE_VENV) || python3 -m venv $(LLM_SERVICE_VENV)
	$(LLM_SERVICE_PIP) install --upgrade pip
	$(LLM_SERVICE_PIP) install -r llm_service/requirements.txt
	@touch $(LLM_SERVICE_VENV)/.installed

ollama-service-up: $(LLM_SERVICE_VENV)/.installed
	@if [ -f $(LLM_SERVICE_PID) ] && kill -0 $$(cat $(LLM_SERVICE_PID)) 2>/dev/null; then \
	  echo "llm_service already running (pid $$(cat $(LLM_SERVICE_PID)))"; exit 0; \
	fi
	# Ollama install check + qwen3:8b auto-pull are done by the service
	# itself on startup (see llm_service/bootstrap.py). First-time launch
	# will block while the ~5 GB model downloads; tail the log to watch.
	@nohup $(LLM_SERVICE_UVI) llm_service.main:app \
	  --host 0.0.0.0 --port $(LLM_SERVICE_PORT) \
	  >$(LLM_SERVICE_LOG) 2>&1 & echo $$! >$(LLM_SERVICE_PID)
	@printf "Waiting for llm_service (model pull may take a few minutes on first run) "
	@for i in $$(seq 1 600); do \
	  curl -sf http://localhost:$(LLM_SERVICE_PORT)/healthz >/dev/null 2>&1 && { echo " ok"; break; }; \
	  if ! kill -0 $$(cat $(LLM_SERVICE_PID)) 2>/dev/null; then \
	    echo " process died — see $(LLM_SERVICE_LOG)"; exit 1; \
	  fi; \
	  printf "."; sleep 1; \
	done
	@echo "llm_service ready at http://localhost:$(LLM_SERVICE_PORT)"
	@echo "  logs: $(LLM_SERVICE_LOG)    pid:  $$(cat $(LLM_SERVICE_PID))"

ollama-service-down:
	@if [ -f $(LLM_SERVICE_PID) ]; then \
	  pid=$$(cat $(LLM_SERVICE_PID)); \
	  kill $$pid 2>/dev/null || true; \
	  rm -f $(LLM_SERVICE_PID); \
	  echo "llm_service stopped (pid $$pid)"; \
	else \
	  echo "no pid file at $(LLM_SERVICE_PID) — nothing to stop"; \
	fi

ollama-service-logs:
	@test -f $(LLM_SERVICE_LOG) || { echo "no log at $(LLM_SERVICE_LOG) — run 'make ollama-service-up' first"; exit 1; }
	tail -f $(LLM_SERVICE_LOG)

# Nuke the llm_service venv so the next ollama-service-up rebuilds it from
# scratch. Useful if requirements.txt drifted or the venv got corrupted.
ollama-service-clean:
	rm -rf $(LLM_SERVICE_VENV) $(LLM_SERVICE_PID) $(LLM_SERVICE_LOG)
