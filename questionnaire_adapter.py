"""
Questionnaire adapter.

For forms like the Invest Europe ESG DDQ where:
  - Fields are AcroForm widgets named "Answer Field N"
  - The actual question text sits ABOVE the field (multiple lines)
  - User data comes as a nested JSON {section -> category -> topic -> questions[]}

This adapter:
  1. Enriches each detected field with the question text extracted from
     the region above the field.
  2. Flattens the nested user_data into { question_id: answer } and indexes
     by both id and question text.
  3. Matches fields to questions by fuzzy text similarity, producing a flat
     user_data compatible with form_filler.py.

Usage:
    python3 questionnaire_adapter.py \\
        --pdf form.pdf \\
        --fields fields_normalized.json \\
        --nested nested_data.json \\
        --out flat_user_data.json
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber


# --------------------------------------------------------------------------- #
# Step 1 — extract question text above each field
# --------------------------------------------------------------------------- #

def _question_text_above(page, bbox: tuple, max_lines: int = 6,
                         max_vgap: float = 10.0) -> str:
    """
    Collect up to `max_lines` of text ending just above the field.
    Stops when the vertical gap between consecutive lines exceeds max_vgap
    (indicating a paragraph break) or when the x-extent shifts significantly
    (indicating a new column).
    """
    x0, top, x1, bottom = bbox

    # Pull all words on the page, then filter to those above the field
    # AND within a reasonable horizontal band (the question column).
    words = page.extract_words(x_tolerance=2, y_tolerance=2)

    # We look for words whose bottom edge is above the field's top,
    # within ~130pt vertically (enough to cover multi-line questions).
    col_x0 = max(0, x0 - 5)       # allow slight overlap
    col_x1 = x1 + 5
    # The question text column usually starts around field x0.
    # But labels in the left margin (e.g. "Business overview") are outside.
    # Include only words whose x-range overlaps the field's x-range.
    candidates = [
        w for w in words
        if w["bottom"] <= top + 1
        and (top - w["bottom"]) < 130
        and w["x1"] > col_x0
        and w["x0"] < col_x1
    ]
    if not candidates:
        return ""

    # Group into lines
    candidates.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in candidates:
        if lines and abs(lines[-1][0]["top"] - w["top"]) < 3:
            lines[-1].append(w)
        else:
            lines.append([w])

    # Sort lines bottom-up (closest first)
    lines.sort(key=lambda ln: -ln[0]["top"])

    collected: list[list[dict]] = []
    prev_top = None
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        line_top = ln[0]["top"]
        if prev_top is not None:
            # Gap between this line and the PREVIOUS (vertically lower) line
            vgap = prev_top - ln[0]["bottom"]
            if vgap > max_vgap:
                break
        collected.append(ln)
        prev_top = line_top
        if len(collected) >= max_lines:
            break

    # Reverse to reading order (top-down)
    collected.reverse()
    text = " ".join(
        " ".join(w["text"] for w in ln) for ln in collected
    )
    return re.sub(r"\s+", " ", text).strip()


def enrich_labels_with_questions(pdf_path: str | Path,
                                 fields_json: str | Path,
                                 out_path: str | Path) -> dict:
    """
    Read fields_normalized.json, replace each field's label with the question
    text extracted from above the field, and write a new JSON.
    """
    data = json.loads(Path(fields_json).read_text())

    with pdfplumber.open(pdf_path) as pdf:
        for f in data["fields"]:
            page = pdf.pages[f["page"] - 1]
            question = _question_text_above(page, tuple(f["bbox"]))
            if question:
                f["question_text"] = question
                # Also improve the label if it was generic
                if f["label"].lower().startswith("answer field") \
                        or f["label"] == "UNKNOWN":
                    f["label"] = question[:80]

    Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# --------------------------------------------------------------------------- #
# Step 2 — flatten nested user data
# --------------------------------------------------------------------------- #

def _flatten_questions(node, path: list[str] | None = None
                       ) -> list[dict]:
    """
    Walk an arbitrary nested dict/list structure and yield any item that
    has both a 'question' (or 'text') and an 'id' field. Collects the path
    for debugging.
    """
    path = path or []
    out: list[dict] = []

    if isinstance(node, dict):
        # Recognize a question node by keys
        if "question" in node and ("id" in node or "qid" in node):
            out.append({
                "id": str(node.get("id") or node.get("qid")),
                "question": str(node["question"]),
                "answer": node.get("answer", ""),
                "path": list(path),
            })
            return out
        for k, v in node.items():
            out.extend(_flatten_questions(v, path + [str(k)]))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_flatten_questions(v, path + [str(i)]))
    return out


def flatten_nested_data(nested: dict) -> list[dict]:
    """Return a flat list of { id, question, answer, path }."""
    return _flatten_questions(nested)


# --------------------------------------------------------------------------- #
# Step 3 — fuzzy match fields to questions
# --------------------------------------------------------------------------- #

def _normalize_for_match(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return s.strip()


# Common words that add noise to token matching
_STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "in", "for", "on", "at",
    "and", "or", "if", "so", "as", "by", "be", "do", "does", "have", "has",
    "any", "your", "please", "provide", "details", "e", "g", "i", "etc",
    "within", "with", "this", "that", "these", "those",
}


def _tokens(s: str) -> set[str]:
    return {w for w in _normalize_for_match(s).split()
            if len(w) > 2 and w not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    """
    Asymmetric token similarity that handles length differences well.

    Returns the fraction of the shorter text's content tokens present in
    the longer text. This suits the case where one side is a condensed
    question ("What is your view on ESG maturity?") and the other is the
    full text with explanations ("What is your view on ESG maturity,
    where 1=mature, 2=partly developed...").
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        # Fall back to character-level ratio for very short strings
        return SequenceMatcher(None, _normalize_for_match(a),
                               _normalize_for_match(b)).ratio()
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    overlap = len(shorter & longer)
    return overlap / len(shorter)


def match_fields_to_questions(
    fields: list[dict],
    questions: list[dict],
    min_similarity: float = 0.55,
) -> dict:
    """
    Return { canonical_key: question_id } mapping.

    Algorithm:
      - Filter fields to those detected via AcroForm or with a question_text.
      - Sort fields by page/position (reading order).
      - Sort questions by id.
      - Greedy match: for each field in order, find the best unassigned
        question by similarity within a sliding window of ±5 positions.
        This handles small misalignments while preserving order (questions
        are laid out top-to-bottom on the page and IDs increase monotonically).
    """
    # Preserve natural reading order
    ordered_fields = sorted(
        fields,
        key=lambda f: (f["page"], f["bbox"][1], f["bbox"][0]),
    )

    def qid_key(qid: str) -> tuple:
        # Convert "1.10.2" -> (1, 10, 2) for natural ordering
        try:
            return tuple(int(x) for x in qid.split("."))
        except ValueError:
            return (qid,)

    ordered_questions = sorted(questions, key=lambda q: qid_key(q["id"]))

    mapping: dict[str, str] = {}
    used_question_ids: set[str] = set()

    q_idx = 0
    for f in ordered_fields:
        q_text = f.get("question_text") or f.get("label", "")
        if not q_text:
            continue

        # Sliding window search
        window_start = max(0, q_idx - 3)
        window_end = min(len(ordered_questions), q_idx + 8)

        best_score = 0.0
        best_q = None
        for i in range(window_start, window_end):
            q = ordered_questions[i]
            if q["id"] in used_question_ids:
                continue
            score = _similarity(q_text, q["question"])
            if score > best_score:
                best_score = score
                best_q = (i, q)

        if best_q and best_score >= min_similarity:
            i, q = best_q
            mapping[f["canonical_key"]] = q["id"]
            used_question_ids.add(q["id"])
            q_idx = i + 1   # advance window

    return mapping


def build_flat_user_data(
    fields: list[dict],
    nested_data: dict,
    mapping: dict[str, str] | None = None,
    question_to_answer: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """
    Returns (flat_user_data, diagnostics).

    flat_user_data: { canonical_key: answer_string }
    diagnostics:    { canonical_key: {question_id, question, matched_score} }
    """
    questions = flatten_nested_data(nested_data)

    # Optionally override answers from an external dict
    by_id = {q["id"]: q for q in questions}
    if question_to_answer:
        for qid, ans in question_to_answer.items():
            if qid in by_id:
                by_id[qid]["answer"] = ans

    if mapping is None:
        mapping = match_fields_to_questions(fields, questions)

    flat: dict[str, str] = {}
    diag: dict[str, dict] = {}
    for f in fields:
        key = f["canonical_key"]
        qid = mapping.get(key)
        if not qid:
            continue
        q = by_id.get(qid)
        if not q:
            continue
        answer = q.get("answer", "")
        if answer:
            flat[key] = str(answer)
        diag[key] = {
            "question_id": qid,
            "question": q["question"][:100],
            "has_answer": bool(answer),
        }

    return flat, diag


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--fields", required=True,
                    help="fields_normalized.json from the normalizer")
    ap.add_argument("--nested", required=True,
                    help="Nested JSON of questions (with answers if any)")
    ap.add_argument("--out", default="flat_user_data.json")
    ap.add_argument("--enriched-fields", default=None,
                    help="Also write back enriched fields with question_text")
    ap.add_argument("--answers", default=None,
                    help="Optional JSON { question_id: answer } overrides")
    ap.add_argument("--diagnostics", default=None,
                    help="Write match diagnostics JSON here")
    args = ap.parse_args()

    # Step 1: enrich labels with question text
    enriched_out = args.enriched_fields or args.fields.replace(
        ".json", "_enriched.json"
    )
    enriched = enrich_labels_with_questions(args.pdf, args.fields, enriched_out)

    # Step 2 + 3: flatten + match
    nested = json.loads(Path(args.nested).read_text())
    overrides = None
    if args.answers:
        overrides = json.loads(Path(args.answers).read_text())

    flat, diag = build_flat_user_data(
        enriched["fields"], nested, question_to_answer=overrides,
    )

    Path(args.out).write_text(json.dumps(flat, indent=2, ensure_ascii=False))
    if args.diagnostics:
        Path(args.diagnostics).write_text(
            json.dumps(diag, indent=2, ensure_ascii=False)
        )

    total_fields = len(enriched["fields"])
    matched = len(diag)
    with_answers = sum(1 for d in diag.values() if d["has_answer"])
    print(f"Fields: {total_fields}  matched: {matched}  "
          f"with answers: {with_answers}  -> {args.out}")
    print(f"Enriched fields: {enriched_out}")
    if args.diagnostics:
        print(f"Diagnostics: {args.diagnostics}")


if __name__ == "__main__":
    main()
