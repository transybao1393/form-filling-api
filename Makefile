# Form Field Detection & Filling Pipeline
#
# Folder convention:
#   input/<name>/
#     *.pdf              # the form (any filename)
#     data.json          # flat, flat-list, or nested JSON (auto-detected)
#     answers.json       # optional: {question_id: answer} for nested schemas
#
#   output/<name>/     # all generated files land here
#
# Usage:
#   make setup
#   make run NAME=<name>          # single command — auto-detects format
#   make list                     # list all available NAMEs

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
        list clean clean-all distclean

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
