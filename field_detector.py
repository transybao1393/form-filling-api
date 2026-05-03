"""
Multi-strategy form field detector.

Runs ALL strategies on the PDF and merges results, so it works on:

  1. AcroForm PDFs      — native form fields (highest priority, 100% accuracy)
  2. Underscore forms   — like ICS: _______ as input slots
  3. Rectangle forms    — input areas bordered by rect primitives
  4. Colon forms        — "Label:" followed by blank writable space
  5. Table cell forms   — label in left cell, input in right cell

Output JSON schema adds two fields vs v1:
  - strategy: which detector found this field
  - field_type: text | checkbox | radio | signature
  - acroform_name: original PDF field name (if from AcroForm)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import pdfplumber
from pypdf import PdfReader


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class FieldRegion:
    field_id: str
    label: str
    page: int
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    confidence: float
    strategy: str
    field_type: str = "text"
    acroform_name: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        return d


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _bbox_iou(a: tuple, b: tuple) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[_:\.]+$", "", text).strip()
    text = re.sub(r"^[_:]+", "", text).strip()
    text = re.sub(r"^(\d+[\.\)]\s*|\([a-z0-9]+\)\s*|[•·▪▫►]\s*)", "", text)
    return text.strip()


def _extract_text_tokens(page) -> list[dict]:
    words = page.extract_words(
        x_tolerance=2, y_tolerance=2,
        keep_blank_chars=False, use_text_flow=True,
    )
    tokens = []
    for w in words:
        cleaned = re.sub(r"_+", "", w["text"]).strip()
        if not cleaned:
            continue
        tokens.append({
            "text": cleaned,
            "x0": w["x0"], "top": w["top"],
            "x1": w["x1"], "bottom": w["bottom"],
        })
    return tokens


def _nearest_label(bbox, tokens: list[dict], prefer: str = "left"
                   ) -> tuple[str, float]:
    """Find best label for a field bbox. prefer ∈ {'left','above','auto'}."""
    x0, y0, x1, y1 = bbox
    cy = (y0 + y1) / 2

    candidates: list[tuple[float, str, str]] = []   # (score, direction, text)

    # LEFT neighbours on same line
    same_line = [
        t for t in tokens
        if abs(((t["top"] + t["bottom"]) / 2) - cy) < max(6, (y1 - y0) / 2 + 2)
        and t["x1"] <= x0 + 2
    ]
    if same_line:
        same_line.sort(key=lambda t: t["x0"])
        tail = []
        for t in reversed(same_line):
            if not tail:
                tail.append(t); continue
            if tail[-1]["x0"] - t["x1"] < 15:
                tail.append(t)
            else:
                break
        tail.reverse()
        if tail:
            text = _clean_label(" ".join(t["text"] for t in tail))
            gap = x0 - tail[-1]["x1"]
            score = 100 - gap
            candidates.append((score, "left", text))

    # ABOVE tokens (horizontally overlapping)
    above = [
        t for t in tokens
        if t["bottom"] <= y0 + 1
        and (y0 - t["bottom"]) < 30
        and t["x1"] > x0 - 5 and t["x0"] < x1 + 5
    ]
    if above:
        above.sort(key=lambda t: -t["bottom"])
        line_y = above[0]["bottom"]
        line_tokens = [t for t in above if abs(t["bottom"] - line_y) < 3]
        line_tokens.sort(key=lambda t: t["x0"])
        text = _clean_label(" ".join(t["text"] for t in line_tokens))
        vgap = y0 - line_y
        score = 80 - vgap * 2
        candidates.append((score, "above", text))

    if not candidates:
        return "UNKNOWN", 0.2

    def bias(direction: str) -> float:
        if prefer == "left":
            return 10 if direction == "left" else 0
        if prefer == "above":
            return 10 if direction == "above" else 0
        return 0

    candidates.sort(key=lambda c: -(c[0] + bias(c[1])))
    best_score, _, best_text = candidates[0]
    if not best_text or best_text.lower() in {"and", "or", "the"}:
        return "UNKNOWN", 0.2
    conf = min(0.95, max(0.3, best_score / 100))
    return best_text, conf


# --------------------------------------------------------------------------- #
# Strategy 1 — AcroForm (native PDF form fields)
# --------------------------------------------------------------------------- #

def _detect_acroform(pdf_path: Path) -> list[FieldRegion]:
    results: list[FieldRegion] = []
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return results

    if not (reader.get_fields() or {}):
        return results

    for page_index, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        try:
            annot_list = annots if isinstance(annots, list) else annots.get_object()
        except Exception:
            continue

        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        for idx, annot_ref in enumerate(annot_list):
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            if annot.get("/Subtype") != "/Widget":
                continue

            rect = annot.get("/Rect")
            if not rect:
                continue

            rl_x0, rl_y0, rl_x1, rl_y1 = [float(v) for v in rect]
            # Convert PDF bottom-left to pdfplumber top-left
            bbox = (rl_x0, page_h - rl_y1, rl_x1, page_h - rl_y0)

            field_name = str(annot.get("/T") or annot.get("/TU")
                             or f"acroform_field_{idx}")

            ft = annot.get("/FT")
            if ft == "/Btn":
                flags = int(annot.get("/Ff") or 0)
                if flags & 32768:
                    ftype = "radio"
                elif flags & 65536:
                    ftype = "text"
                else:
                    ftype = "checkbox"
            elif ft == "/Sig":
                ftype = "signature"
            else:
                ftype = "text"

            label = _clean_label(str(annot.get("/TU") or field_name)) or field_name

            results.append(FieldRegion(
                field_id=f"page{page_index}_acroform_{idx:03d}",
                label=label,
                page=page_index,
                bbox=bbox,
                page_width=page_w,
                page_height=page_h,
                confidence=1.0,
                strategy="acroform",
                field_type=ftype,
                acroform_name=field_name,
            ))
    return results


# --------------------------------------------------------------------------- #
# Strategy 2 — Underscore runs
# --------------------------------------------------------------------------- #

def _detect_underscore(page) -> list[dict]:
    chars = [c for c in page.chars if c["text"] == "_"]
    if not chars:
        return []
    chars.sort(key=lambda c: (round(c["top"], 1), c["x0"]))

    slots, current = [], []

    def flush():
        if not current:
            return
        slot = {
            "x0": min(c["x0"] for c in current),
            "top": min(c["top"] for c in current),
            "x1": max(c["x1"] for c in current),
            "bottom": max(c["bottom"] for c in current),
        }
        if slot["x1"] - slot["x0"] >= 15:
            slots.append(slot)
        current.clear()

    for c in chars:
        if not current:
            current.append(c); continue
        prev = current[-1]
        if abs(c["top"] - prev["top"]) < 2.0 and (c["x0"] - prev["x1"]) < 3.0:
            current.append(c)
        else:
            flush()
            current.append(c)
    flush()
    return slots


# --------------------------------------------------------------------------- #
# Strategy 3 — Rectangle-bordered input areas
# --------------------------------------------------------------------------- #

def _detect_rectangles(page) -> list[dict]:
    page_area = page.width * page.height
    candidates = []
    for r in page.rects:
        x0, top, x1, bottom = r["x0"], r["top"], r["x1"], r["bottom"]
        w, h = x1 - x0, bottom - top
        if w < 30 or h < 8 or h > 80:
            continue
        if w * h > page_area * 0.35:
            continue
        # Skip filled shapes (not input boxes)
        fill = r.get("fill")
        ns_color = r.get("non_stroking_color")
        if fill and ns_color not in (None, (1, 1, 1), 1):
            continue

        # Empty inside?
        chars_inside = [
            c for c in page.chars
            if c["x0"] >= x0 - 1 and c["x1"] <= x1 + 1
            and c["top"] >= top - 1 and c["bottom"] <= bottom + 1
        ]
        non_ws = [c for c in chars_inside if c["text"].strip() and c["text"] != "_"]
        if len(non_ws) > 3:
            continue

        candidates.append({"x0": x0, "top": top, "x1": x1, "bottom": bottom})
    return candidates


# --------------------------------------------------------------------------- #
# Strategy 4 — Colon-based fields
# --------------------------------------------------------------------------- #

def _detect_colon_fields(page, tokens: list[dict]) -> list[dict]:
    slots = []
    for i, t in enumerate(tokens):
        is_colon = t["text"].rstrip().endswith(":")
        if not is_colon and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if (nxt["text"] == ":"
                    and abs(nxt["top"] - t["top"]) < 3
                    and (nxt["x0"] - t["x1"]) < 10):
                is_colon = True

        if not is_colon:
            continue

        colon_x1 = t["x1"]
        line_mid_y = (t["top"] + t["bottom"]) / 2
        right_limit = page.width - 40

        for other in tokens:
            if other is t:
                continue
            if (abs(((other["top"] + other["bottom"]) / 2) - line_mid_y) < 4
                    and other["x0"] > colon_x1 + 5
                    and other["x0"] < right_limit):
                right_limit = other["x0"] - 5
                break

        slot_x0 = colon_x1 + 3
        slot_x1 = right_limit
        if slot_x1 - slot_x0 < 40:
            continue

        slots.append({
            "x0": slot_x0, "top": t["top"],
            "x1": slot_x1, "bottom": t["bottom"],
        })
    return slots


# --------------------------------------------------------------------------- #
# Strategy 5 — Table cells
# --------------------------------------------------------------------------- #

def _detect_table_cells(page) -> list[dict]:
    slots = []
    try:
        tables = page.find_tables()
    except Exception:
        return slots

    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                if cell is None:
                    continue
                x0, top, x1, bottom = cell
                w, h = x1 - x0, bottom - top
                if w < 30 or h < 10 or h > 120:
                    continue
                cell_chars = [
                    c for c in page.chars
                    if c["x0"] >= x0 and c["x1"] <= x1
                    and c["top"] >= top and c["bottom"] <= bottom
                ]
                text = "".join(c["text"] for c in cell_chars).strip()
                if text:
                    continue
                slots.append({
                    "x0": x0, "top": top, "x1": x1, "bottom": bottom,
                })
    return slots


# --------------------------------------------------------------------------- #
# Dedupe across strategies
# --------------------------------------------------------------------------- #

STRATEGY_PRIORITY = {
    "acroform": 5,
    "table": 4,
    "rectangle": 3,
    "underscore": 2,
    "colon": 1,
}


def _dedupe(fields: list[FieldRegion], iou_threshold: float = 0.3
            ) -> list[FieldRegion]:
    fields_sorted = sorted(
        fields,
        key=lambda f: (-STRATEGY_PRIORITY.get(f.strategy, 0), -f.confidence),
    )
    kept: list[FieldRegion] = []
    for f in fields_sorted:
        if any(k.page == f.page and _bbox_iou(f.bbox, k.bbox) > iou_threshold
               for k in kept):
            continue
        kept.append(f)
    kept.sort(key=lambda f: (f.page, f.bbox[1], f.bbox[0]))
    for i, f in enumerate(kept):
        f.field_id = f"page{f.page}_field_{i:03d}"
    return kept


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def detect_fields(
    pdf_path: str | Path,
    strategies: list[str] | None = None,
) -> list[FieldRegion]:
    pdf_path = Path(pdf_path)
    if strategies is None:
        strategies = ["acroform", "underscore", "rectangle", "colon", "table"]

    all_fields: list[FieldRegion] = []

    if "acroform" in strategies:
        all_fields.extend(_detect_acroform(pdf_path))

    # If AcroForm gave us a full match, often that's all we need — but we
    # still run other strategies in case of hybrid forms.
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tokens = _extract_text_tokens(page)

            def add_slots(slots, strategy_name, prefer="left"):
                for slot in slots:
                    bbox = (slot["x0"], slot["top"], slot["x1"], slot["bottom"])
                    label, conf = _nearest_label(bbox, tokens, prefer=prefer)
                    all_fields.append(FieldRegion(
                        field_id=f"page{page_num}_{strategy_name}_{len(all_fields):03d}",
                        label=label,
                        page=page_num,
                        bbox=bbox,
                        page_width=page.width,
                        page_height=page.height,
                        confidence=conf,
                        strategy=strategy_name,
                    ))

            if "underscore" in strategies:
                add_slots(_detect_underscore(page), "underscore")
            if "rectangle" in strategies:
                add_slots(_detect_rectangles(page), "rectangle", prefer="auto")
            if "colon" in strategies:
                add_slots(_detect_colon_fields(page, tokens), "colon")
            if "table" in strategies:
                add_slots(_detect_table_cells(page), "table")

    return _dedupe(all_fields)


def detect_fields_to_json(
    pdf_path: str | Path,
    out_path: str | Path,
    strategies: list[str] | None = None,
) -> dict:
    fields = detect_fields(pdf_path, strategies=strategies)

    by_strategy: dict[str, int] = {}
    for f in fields:
        by_strategy[f.strategy] = by_strategy.get(f.strategy, 0) + 1

    payload = {
        "source_pdf": str(pdf_path),
        "num_fields": len(fields),
        "fields_by_strategy": by_strategy,
        "fields": [f.to_dict() for f in fields],
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--output", default="fields.json")
    ap.add_argument("--strategies", nargs="+", default=None,
                    choices=["acroform", "underscore", "rectangle",
                             "colon", "table"],
                    help="Subset of strategies to run (default: all)")
    args = ap.parse_args()

    data = detect_fields_to_json(args.pdf, args.output, strategies=args.strategies)
    print(f"Detected {data['num_fields']} fields → {args.output}")
    print(f"By strategy: {data['fields_by_strategy']}")
    for f in data["fields"][:15]:
        bb = f["bbox"]
        print(f"  [{f['confidence']:.2f}] ({f['strategy']}/{f['field_type']}) "
              f"p{f['page']} '{f['label'][:40]}' "
              f"bbox=({bb[0]:.0f},{bb[1]:.0f},{bb[2]:.0f},{bb[3]:.0f})")
    if data['num_fields'] > 15:
        print(f"  ... and {data['num_fields'] - 15} more")
