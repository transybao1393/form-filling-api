"""
Generic field normalizer.

Unlike v1, this does NOT require a form-specific mapping table. It derives
canonical keys automatically:

  1. Section detection: scan each page for heading-like lines (all caps, bold,
     larger font, or centered). Each field's section = last heading above it.
  2. Label → key: lowercase, strip punctuation, replace spaces with _,
     prefix with section slug if present.
  3. Deduplicate: append _1, _2 for fields whose key would collide on the same
     page/section.
  4. For AcroForm fields: keep the original field name as canonical_key
     (since PDF authors already chose a stable identifier).

Users can STILL override the mapping with an optional custom_mapping.json:

  {
    "overrides": {
      "page1_field_003": "office_address",    // override by field_id
      "first_name in general_information": "first_name"  // by (section, label)
    },
    "sections": [
      {"pattern": "general\\s+information", "name": "general_information"}
    ]
  }
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pdfplumber


# Built-in section patterns — generic ones that cover most professional forms.
BUILTIN_SECTION_PATTERNS = [
    (re.compile(r"general\s+information", re.I), "general_information"),
    (re.compile(r"personal\s+information", re.I), "personal_information"),
    (re.compile(r"professional\s+information", re.I), "professional_information"),
    (re.compile(r"contact\s+information", re.I), "contact_information"),
    (re.compile(r"employment\s+(history|information)", re.I), "employment"),
    (re.compile(r"education", re.I), "education"),
    (re.compile(r"references", re.I), "references"),
    (re.compile(r"(payment|application\s+fee)", re.I), "payment"),
    (re.compile(r"authorization", re.I), "authorization"),
    (re.compile(r"signature", re.I), "signature"),
    # ESG / due-diligence style
    (re.compile(r"company\s+(information|details|profile)", re.I), "company"),
    (re.compile(r"governance", re.I), "governance"),
    (re.compile(r"environmental", re.I), "environmental"),
    (re.compile(r"social", re.I), "social"),
    (re.compile(r"risk\s+management", re.I), "risk_management"),
    (re.compile(r"compliance", re.I), "compliance"),
    (re.compile(r"supply\s+chain", re.I), "supply_chain"),
    (re.compile(r"data\s+protection", re.I), "data_protection"),
]


def _cluster_lines(chars: list[dict], tol: float = 6.0) -> list[list[dict]]:
    """Group chars into lines using a vertical tolerance (handles small-caps)."""
    chars = sorted(chars, key=lambda c: (c["top"], c["x0"]))
    clusters: list[list[dict]] = []
    for c in chars:
        if clusters and abs(c["top"] - clusters[-1][0]["top"]) < tol:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    for cluster in clusters:
        cluster.sort(key=lambda c: c["x0"])
    return clusters


def _is_heading_line(cluster: list[dict], page_width: float) -> bool:
    """
    Heuristic: a line looks like a heading if it is short, bold/large, or
    centred, OR if it matches a known built-in pattern.
    """
    text = "".join(c["text"] for c in cluster).strip()
    compact = re.sub(r"\s+", "", text).lower()
    if not compact:
        return False

    # Known pattern match
    for pat, _ in BUILTIN_SECTION_PATTERNS:
        target = re.sub(r"\\s\+", "", pat.pattern).lower()
        target = re.sub(r"[\^\$\\]", "", target)
        if target and target in compact:
            return True

    # Otherwise: all caps, short, and centered
    if len(text) > 50:
        return False
    if text.upper() != text:
        return False
    # Centered? Compute horizontal midpoint
    x0 = min(c["x0"] for c in cluster)
    x1 = max(c["x1"] for c in cluster)
    mid = (x0 + x1) / 2
    if abs(mid - page_width / 2) < page_width * 0.15:
        return True
    return False


def _section_name_from_line(cluster: list[dict], user_patterns: list) -> str:
    text = "".join(c["text"] for c in cluster).strip()
    compact = re.sub(r"\s+", "", text).lower()

    for pat, name in list(user_patterns) + BUILTIN_SECTION_PATTERNS:
        target = re.sub(r"\\s\+", "", pat.pattern).lower()
        target = re.sub(r"[\^\$\\]", "", target)
        if target and target in compact:
            return name

    # Fallback: slugify the heading text itself
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "section"


def _detect_section_bands(pdf_path: Path, user_patterns: list
                          ) -> dict[int, list[tuple[float, str]]]:
    bands: dict[int, list[tuple[float, str]]] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            page_bands: list[tuple[float, str]] = []
            if not page.chars:
                bands[page_num] = []
                continue

            clusters = _cluster_lines(page.chars, tol=6.0)
            for cluster in clusters:
                if _is_heading_line(cluster, page.width):
                    section_name = _section_name_from_line(cluster, user_patterns)
                    y = min(c["top"] for c in cluster)
                    page_bands.append((y, section_name))

            bands[page_num] = sorted(page_bands, key=lambda b: b[0])
    return bands


def _section_for(y: float, page_bands: list[tuple[float, str]]) -> str:
    current = "general"
    for band_y, name in page_bands:
        if y >= band_y:
            current = name
        else:
            break
    return current


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "field"


def normalize_fields(
    pdf_path: str | Path,
    raw_fields: list[dict],
    custom_mapping: dict | None = None,
) -> list[dict]:
    pdf_path = Path(pdf_path)
    custom_mapping = custom_mapping or {}
    overrides: dict = custom_mapping.get("overrides", {})

    user_patterns = [
        (re.compile(s["pattern"], re.I), s["name"])
        for s in custom_mapping.get("sections", [])
    ]

    bands = _detect_section_bands(pdf_path, user_patterns)

    # Pass 1: assign section + raw candidate key
    enriched: list[dict] = []
    for f in raw_fields:
        section = _section_for(f["bbox"][1], bands.get(f["page"], []))

        # AcroForm fields keep their PDF-assigned name
        if f.get("strategy") == "acroform" and f.get("acroform_name"):
            candidate = _slugify(f["acroform_name"])
        else:
            label = f.get("label", "").strip()
            if not label or label.upper() == "UNKNOWN":
                candidate = f["field_id"]  # fallback to detector id
            else:
                candidate = _slugify(label)

        if section and section != "general":
            candidate = f"{section}__{candidate}"

        # Apply overrides
        override_key = overrides.get(f["field_id"])
        if not override_key:
            override_key = overrides.get(f"{f.get('label', '')} in {section}")
        if override_key:
            candidate = override_key

        out = dict(f)
        out["section"] = section
        out["canonical_key"] = candidate
        enriched.append(out)

    # Pass 2: dedupe colliding keys with _1, _2 suffixes
    counts: dict[str, int] = {}
    seen: dict[str, int] = {}
    for f in enriched:
        counts[f["canonical_key"]] = counts.get(f["canonical_key"], 0) + 1

    for f in enriched:
        key = f["canonical_key"]
        if counts[key] > 1:
            seen[key] = seen.get(key, 0) + 1
            f["canonical_key"] = f"{key}_{seen[key]}"

    return enriched


def enrich_json(in_path: str | Path, out_path: str | Path,
                custom_mapping_path: str | Path | None = None) -> dict:
    data = json.loads(Path(in_path).read_text())
    custom = None
    if custom_mapping_path and Path(custom_mapping_path).exists():
        custom = json.loads(Path(custom_mapping_path).read_text())
    data["fields"] = normalize_fields(data["source_pdf"], data["fields"], custom)
    Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("input_json")
    ap.add_argument("-o", "--output", default="fields_normalized.json")
    ap.add_argument("-m", "--mapping",
                    help="Optional custom_mapping.json for overrides")
    args = ap.parse_args()

    data = enrich_json(args.input_json, args.output, args.mapping)
    print(f"Normalized {data['num_fields']} fields → {args.output}")
    for f in data["fields"]:
        print(f"  [{f.get('section','?'):20s}] {f['canonical_key']:45s}"
              f"  ← '{f['label'][:30]}' ({f['strategy']})")
