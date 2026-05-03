# Form Field Detection & Filling Pipeline

Generic pipeline that fills PDF forms from JSON data. One command handles
**any form + data format combination** — no need to know which adapter to run.

## Quick start

```bash
make setup                    # one-time: install deps into .venv/
make run NAME=my_form         # run pipeline
```

That's it. The pipeline auto-detects the data format and dispatches to the
right adapter.

## Folder convention

```
input/<n>/
    anything.pdf        # the form — any filename, any format
    data.json           # user data — any of the supported formats (auto-detected)
    answers.json        # OPTIONAL — flat {question_id: answer} for nested schemas

output/<n>/          # all generated files land here
    fields.json
    fields_normalized.json
    fields_enriched.json      (if adapter was used)
    filled.pdf
    diagnostics.json          (for questionnaire modes)
    debug.png                 (if you ran `make visualize`)
```

Use `make list` to see what's available.

## Supported data formats (all auto-detected)

### 1. Flat `{key: value}` — simplest case

```json
{
    "first_name": "Bao",
    "last_name": "Tran",
    "email": "bao@example.com"
}
```

Keys must match the `canonical_key` produced by the normalizer. Use
`make template NAME=<n>` to generate a scaffold with the right keys.

### 2. Flat-list `{ items: [...] }` — OCR'd questionnaires

```json
{
    "items": [
        {
            "question": "Raison sociale",
            "extracted_answer": "SCI FERME DES TEMPLIERS"
        },
        {
            "question": "N° SIRET",
            "extracted_answer": "484 183 926"
        }
    ]
}
```

The adapter extracts labels from the **left** of each field (typical for
French/European insurance forms, tax forms, application forms), then fuzzy-matches
to items by `question` text. Works with any list key name (`items`, `questions`,
`entries`...) and answer key name (`answer`, `extracted_answer`, `value`...).

### 3. Nested structure — ESG DDQs, multi-section questionnaires

```json
{
    "document": {
        "sections": [
            {
                "categories": [
                    {
                        "topics": [
                            {
                                "questions": [
                                    {"id": "1.1.1", "question": "Describe..."}
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
}
```

The adapter walks the tree finding `{id, question}` leaves at any depth,
extracts question text **above** each PDF field (multi-line), and fuzzy-matches
by question content. Pair with `answers.json` (`{question_id: answer}`) to
supply the actual answers separately from the schema.

## How auto-detection works

`run_pipeline.py` inspects your `data.json`:

1. **Flat-list check** — any top-level list (or `items`/`questions`/`entries`
   keys) containing dicts with question-like keys → **flatlist mode**
2. **Nested check** — any `{id, question}` leaves at any depth → **nested mode**
3. **Fallback** — ≥70% of top-level values are scalars → **flat mode**

To override: `make run NAME=<n> FORMAT=flat` (or `flatlist`, `nested`).
Not wired into Makefile by default — use `python3 run_pipeline.py ... --format`.

## Field detection strategies

Runs all 5 in parallel, dedupes overlaps by IoU:

| Strategy    | Priority | Works on                                |
|-------------|----------|-----------------------------------------|
| acroform    | 5        | PDFs with native form fields            |
| table       | 4        | Forms laid out as table cells           |
| rectangle   | 3        | Forms with box-bordered input areas     |
| underscore  | 2        | Forms using `_____` as input slots      |
| colon       | 1        | Forms using `Label:` with blank space   |

## Commands

| Command                      | Does                                           |
|------------------------------|------------------------------------------------|
| `make setup`                 | Create venv + install deps                     |
| `make list`                  | List all `NAME`s in `input/`                   |
| **`make run NAME=<n>`**      | **Run everything** — the main command          |
| `make detect NAME=<n>`       | Stage 1 only (debug)                           |
| `make normalize NAME=<n>`    | Stage 2 only (debug)                           |
| `make fill NAME=<n>`         | Stage 3 only (debug)                           |
| `make visualize NAME=<n>`    | Draw detected bboxes → debug.png               |
| `make template NAME=<n>`     | Generate empty `{canonical_key: ""}` scaffold  |
| `make preview NAME=<n>`      | Render filled PDF → PNG                        |
| `make clean NAME=<n>`        | Delete `output/<n>/`                           |
| `make clean-all`             | Delete all of `output/`                        |
| `make distclean`             | clean-all + remove venv                        |

## Install

```bash
make setup
```

For `visualize` and `preview` features you also need poppler:
- Linux: `sudo apt-get install poppler-utils`
- macOS: `brew install poppler`

## Files

| File                        | Role                                           |
|-----------------------------|------------------------------------------------|
| `run_pipeline.py`           | Smart dispatcher — detects format, runs stages |
| `field_detector.py`         | Stage 1 — 5-strategy field detection           |
| `field_normalizer.py`       | Stage 2 — canonical key derivation             |
| `flatlist_adapter.py`       | Adapter for flat-list + left-label forms       |
| `questionnaire_adapter.py`  | Adapter for nested + above-label forms         |
| `form_filler.py`            | Stage 3 — render user data onto PDF            |
| `form_utils.py`             | Helpers: template + visualize                  |
| `Makefile`                  | Convenience commands                           |

## Test results

| NAME  | Form                         | Detected as | Fields | Filled                |
|-------|------------------------------|-------------|--------|------------------------|
| ics   | ICS fellowship app           | flat        | 35     | 33/35                  |
| esg   | Invest Europe ESG DDQ        | nested      | 103    | 101 matched, 11 answers|
| test1 | Generali Multirisque 100% Pro| flatlist    | 274    | 48 matched, 23 answers |

## Known limitations

- **Scanned PDFs** need OCR first (pipeline assumes a text layer).
- **Segmented input cells** (e.g. `/_/_/_/` for card numbers) are detected
  as one long slot, not per-character.
- **AcroForm multi-line text** is truncated in native fill mode — use
  overlay mode (default for flat data) if you need word-wrap.
- **Flat-list with answers containing checkbox prefixes** (e.g.
  `"☑ non — explanation..."`) currently writes the full string into text
  fields; could be improved to split checkbox state from explanation.
