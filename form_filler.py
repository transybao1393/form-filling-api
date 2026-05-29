"""
Generic form filler.

Handles different field types:
  - text       → draws value as string, auto-sizes to fit bbox width
  - checkbox   → draws ✓ if value is truthy (True, "yes", "x", 1, ...)
  - radio      → draws ● if value matches the field (see below)
  - signature  → draws value in a script-like font style

Strategies:
  - AcroForm fields can be filled two ways:
      (a) overlay (same as other fields)
      (b) native (pypdf's update_page_form_field_values — preserves form state)
    We default to (a) so the output looks the same for all forms. Pass
    `--acroform-native` to use (b) instead.
  - For multi-line text (bbox height > 20pt), text wraps to multiple lines.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from font_utils import resolve_font, resolve_oblique, _needs_unicode


def _is_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "y", "x", "✓",
                                      "checked", "on", "1"}
    return bool(v)


def _fit_font_size(text: str, max_width: float, base_size: float,
                   font_name: str = "Helvetica") -> float:
    font_name = resolve_font(text, font_name)
    size = base_size
    while size > 6:
        if pdfmetrics.stringWidth(text, font_name, size) <= max_width - 4:
            return size
        size -= 0.5
    return 6.0


def _wrap_text(text: str, max_width: float, size: float,
               font_name: str = "Helvetica") -> list[str]:
    """Naive word-wrap — good enough for form fields."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join(current + [word])
        if pdfmetrics.stringWidth(trial, font_name, size) <= max_width - 4:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_text(c: canvas.Canvas, bbox, page_height: float, value: str,
               font_name: str = "Helvetica", base_size: float = 10.0) -> None:
    x0, top, x1, bottom = bbox
    slot_w = x1 - x0
    slot_h = bottom - top

    # Resolve Unicode-capable font if value contains non-Latin-1 characters
    font_name = resolve_font(value, font_name)

    # Multi-line if slot is tall
    if slot_h > 20:
        size = base_size
        c.setFont(font_name, size)
        lines = _wrap_text(value, slot_w, size, font_name)
        line_height = size * 1.2
        # Start from top of slot, moving down
        for i, line in enumerate(lines):
            if (i + 1) * line_height > slot_h - 2:
                break
            baseline_pdf_y = top + (i + 1) * line_height - 2
            baseline_rl_y = page_height - baseline_pdf_y
            c.drawString(x0 + 2, baseline_rl_y, line)
    else:
        size = _fit_font_size(value, slot_w, base_size, font_name)
        c.setFont(font_name, size)
        baseline_pdf_y = bottom - 1.0
        baseline_rl_y = page_height - baseline_pdf_y
        c.drawString(x0 + 2, baseline_rl_y, value)


def _draw_checkmark(c: canvas.Canvas, bbox, page_height: float) -> None:
    x0, top, x1, bottom = bbox
    size = min(x1 - x0, bottom - top) * 0.9
    cx = (x0 + x1) / 2
    cy = page_height - (top + bottom) / 2
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, cy - size / 3, "✓")


def _draw_radio_dot(c: canvas.Canvas, bbox, page_height: float) -> None:
    x0, top, x1, bottom = bbox
    cx = (x0 + x1) / 2
    cy = page_height - (top + bottom) / 2
    r = min(x1 - x0, bottom - top) * 0.3
    c.circle(cx, cy, r, fill=1, stroke=0)


def _draw_signature(c: canvas.Canvas, bbox, page_height: float, value: str
                    ) -> None:
    """Signature — use italic font for a scripty feel."""
    x0, top, x1, bottom = bbox
    slot_w = x1 - x0
    font_name = resolve_oblique(value, "Helvetica-Oblique")
    size = _fit_font_size(value, slot_w, 12, font_name)
    c.setFont(font_name, size)
    baseline_pdf_y = bottom - 1.0
    baseline_rl_y = page_height - baseline_pdf_y
    c.drawString(x0 + 2, baseline_rl_y, value)


def _remove_widget_by_acroform_name(writer: PdfWriter,
                                    acroform_names: set[str]) -> None:
    """Remove AcroForm widget annotations whose /T matches any name in the set.

    This is needed for fields deferred to the ReportLab overlay path: if the
    widget annotation is left in the PDF, the viewer renders its own appearance
    (placeholder text or corrupted glyphs) on top of the overlay, causing
    double/broken text.
    """
    from pypdf.generic import ArrayObject as _AO

    for page in writer.pages:
        annots_obj = page.get("/Annots")
        if annots_obj is None:
            continue
        # Resolve indirect reference if needed
        annots = annots_obj.get_object() \
            if hasattr(annots_obj, "get_object") else annots_obj
        if not isinstance(annots, _AO):
            continue
        keep = []
        for ref in list(annots):
            annot = ref.get_object() if hasattr(ref, "get_object") else ref
            if annot.get("/Subtype") == "/Widget":
                t = annot.get("/T")
                if t is not None and str(t) in acroform_names:
                    continue  # drop this widget
            keep.append(ref)
        # Replace /Annots in-place with filtered list
        page[NameObject("/Annots")] = _AO(keep)


def _fill_acroform_native(source_pdf: Path, fields: list[dict],
                          user_data: dict, output_pdf: Path) -> dict:
    """Fill AcroForm fields using pypdf's native API (preserves form state).

    Fields whose value contains non-Latin-1 characters (e.g. Vietnamese) are
    NOT filled natively — pypdf encodes them via the PDF's /DA font (usually
    /Helv = Helvetica) which only covers Latin-1, causing silent corruption.
    Those fields are returned in ``needs_overlay`` so the caller can render
    them via the ReportLab overlay path (which uses DejaVu Sans).

    Critically, the widget annotations for deferred fields are REMOVED from
    the output PDF so the viewer does not render its own (broken) appearance
    on top of the ReportLab overlay.
    """
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter(clone_from=reader)

    # Collect updates per page
    updates_by_page: dict[int, dict] = {}
    filled, missing = [], []
    needs_overlay: list[dict] = []   # acroform fields deferred to overlay
    deferred_acroform_names: set[str] = set()  # /T names to remove from /Annots

    for f in fields:
        if f.get("strategy") != "acroform":
            continue
        key = f["canonical_key"]
        if key not in user_data:
            missing.append(key)
            continue
        value = user_data[key]

        # For checkbox, convert truthy to "/Yes" — always safe for native fill
        if f.get("field_type") == "checkbox":
            value = "/Yes" if _is_truthy(value) else "/Off"
            updates_by_page.setdefault(f["page"] - 1, {})[f["acroform_name"]] = str(value)
            filled.append(key)
            continue

        # Defer non-Latin-1 text values to the ReportLab overlay path
        if _needs_unicode(str(value)):
            needs_overlay.append(f)
            deferred_acroform_names.add(f.get("acroform_name", key))
            continue

        updates_by_page.setdefault(f["page"] - 1, {})[f["acroform_name"]] = str(value)
        filled.append(key)

    for page_idx, updates in updates_by_page.items():
        writer.update_page_form_field_values(writer.pages[page_idx], updates)

    # Remove widget annotations for deferred fields so the viewer does not
    # render its own (Helvetica-encoded, broken) appearance over the overlay.
    if deferred_acroform_names:
        _remove_widget_by_acroform_name(writer, deferred_acroform_names)

    # Make sure form fields remain visible
    if "/AcroForm" in writer._root_object:
        writer._root_object["/AcroForm"][NameObject("/NeedAppearances")] = \
            BooleanObject(True)

    with open(output_pdf, "wb") as fh:
        writer.write(fh)

    return {"filled": filled, "missing": missing, "needs_overlay": needs_overlay}


def fill_form(
    source_pdf: str | Path,
    fields_json: str | Path,
    user_data: dict,
    output_pdf: str | Path,
    font_name: str = "Helvetica",
    base_size: float = 10.0,
    acroform_native: bool = False,
    missing_behaviour: str = "skip",
) -> dict:
    source_pdf = Path(source_pdf)
    data = json.loads(Path(fields_json).read_text())
    fields = data["fields"]

    # If requested and we have AcroForm fields, fill them natively first.
    # Fields with non-Latin-1 values are deferred back to the overlay path.
    acroform_report = {"filled": [], "missing": [], "needs_overlay": []}
    if acroform_native and any(f.get("strategy") == "acroform" for f in fields):
        acroform_report = _fill_acroform_native(source_pdf, fields, user_data,
                                                output_pdf)
        source_pdf = Path(output_pdf)  # subsequent overlays go on top

    # Build set of acroform field keys that were handled natively (not deferred)
    native_filled_keys: set[str] = set(acroform_report["filled"])

    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()

    per_page: dict[int, list[dict]] = {}
    for f in fields:
        # Skip AcroForm fields that were successfully filled natively
        if acroform_native and f.get("strategy") == "acroform" \
                and f["canonical_key"] in native_filled_keys:
            continue
        # Include deferred Unicode fields — they go through the overlay path
        per_page.setdefault(f["page"], []).append(f)

    filled = list(acroform_report["filled"])
    missing = list(acroform_report["missing"])
    used_keys: set[str] = set(filled)

    for page_index, page in enumerate(reader.pages, start=1):
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(page_w, page_h))

        for f in per_page.get(page_index, []):
            key = f["canonical_key"]
            if key not in user_data:
                missing.append(key)
                continue
            value = user_data[key]
            ftype = f.get("field_type", "text")
            bbox = tuple(f["bbox"])

            if ftype == "checkbox":
                if _is_truthy(value):
                    _draw_checkmark(c, bbox, page_h)
            elif ftype == "radio":
                if _is_truthy(value):
                    _draw_radio_dot(c, bbox, page_h)
            elif ftype == "signature":
                _draw_signature(c, bbox, page_h, str(value))
            else:  # text
                _draw_text(c, bbox, page_h, str(value), font_name, base_size)

            filled.append(key)
            used_keys.add(key)

        c.save()
        packet.seek(0)
        overlay_pdf = PdfReader(packet)
        if len(overlay_pdf.pages) > 0:
            page.merge_page(overlay_pdf.pages[0])
        writer.add_page(page)

    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    with open(output_pdf, "wb") as fh:
        writer.write(fh)

    unknown_keys = [k for k in user_data if k not in used_keys]

    report = {
        "output_pdf": str(output_pdf),
        "num_filled": len(filled),
        "num_missing": len(missing),
        "filled": filled,
        "missing": missing,
        "unknown_keys_in_user_data": unknown_keys,
    }

    if unknown_keys and missing_behaviour == "warn":
        print(f"[warn] user_data keys not present in form: {unknown_keys[:10]}"
              f"{' ...' if len(unknown_keys) > 10 else ''}")
    if missing_behaviour == "raise" and unknown_keys:
        raise KeyError(f"Unknown user_data keys: {unknown_keys}")

    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("source_pdf")
    ap.add_argument("fields_json")
    ap.add_argument("user_data_json")
    ap.add_argument("-o", "--output", default="filled.pdf")
    ap.add_argument("--acroform-native", action="store_true",
                    help="Use pypdf's native AcroForm fill (preserves form state)")
    args = ap.parse_args()

    user_data = json.loads(Path(args.user_data_json).read_text())
    report = fill_form(
        args.source_pdf, args.fields_json, user_data, args.output,
        acroform_native=args.acroform_native,
        missing_behaviour="warn",
    )
    total = report['num_filled'] + report['num_missing']
    print(f"Filled {report['num_filled']}/{total} fields → {report['output_pdf']}")
    if report["missing"]:
        missing_preview = report['missing'][:10]
        suffix = f" ... (+{len(report['missing']) - 10})" if len(report['missing']) > 10 else ""
        print(f"  missing: {missing_preview}{suffix}")
