"""AcroForm widget injection + carry-over from overlay-filled PDFs.

The form_pipeline detector returns each field's bbox/page/type/canonical_key
even when the source PDF has no native form fields. This module turns that
metadata into real pypdf widget annotations, optionally pre-populated from
either user-supplied data or from text already drawn on the page.

Public API
----------
- has_acroform(pdf_path) -> bool
- count_acroform_fields(pdf_path) -> int
- fill_existing_acroform(source_pdf, fields, user_data, output_pdf) -> dict
- extract_carry_over_values(source_pdf, fields) -> dict[str, str]
- inject_acroform_widgets(source_pdf, fields, user_data, carry_over,
                          output_pdf) -> dict
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


TRUTHY = {True, 1, "1", "true", "yes", "y", "x", "✓", "checked", "on"}


def _is_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "y", "x", "✓",
                                     "checked", "on", "1"}
    return bool(v)


# --------------------------------------------------------------------------- #
# Detection helpers
# --------------------------------------------------------------------------- #

def has_acroform(pdf_path: str | Path) -> bool:
    """Cheap check: does the PDF carry a non-empty AcroForm /Fields array?"""
    try:
        reader = PdfReader(str(pdf_path))
        # get_fields() walks the AcroForm tree and returns a dict — empty
        # dict means no widgets even if /AcroForm is present (e.g. an empty
        # template carrying only /DR resources).
        return bool(reader.get_fields())
    except Exception:
        return False


def count_acroform_fields(pdf_path: str | Path) -> int:
    try:
        reader = PdfReader(str(pdf_path))
        return len(reader.get_fields() or {})
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Carry-over from overlay-filled PDFs
# --------------------------------------------------------------------------- #

def extract_carry_over_values(
    source_pdf: str | Path, fields: list[dict]
) -> dict[str, str]:
    """For each text-typed detected field, return any text already drawn
    inside its bbox (i.e. recovered from a previously overlay-filled PDF).
    Skips checkbox/radio fields — carrying glyphs over as strings makes no
    sense for /Btn widgets.
    """
    import pdfplumber

    out: dict[str, str] = {}
    text_types = {"text", "signature", None}  # None = unknown → assume text

    try:
        with pdfplumber.open(str(source_pdf)) as pdf:
            for f in fields:
                if f.get("field_type") not in text_types:
                    continue
                page_no = f.get("page", 1) - 1
                if page_no < 0 or page_no >= len(pdf.pages):
                    continue
                page = pdf.pages[page_no]
                bbox = f.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x0, top, x1, bottom = bbox
                # Clamp to page edges; pdfplumber raises on out-of-bounds.
                x0 = max(0.0, min(float(x0), page.width))
                x1 = max(0.0, min(float(x1), page.width))
                top = max(0.0, min(float(top), page.height))
                bottom = max(0.0, min(float(bottom), page.height))
                if x1 <= x0 or bottom <= top:
                    continue
                try:
                    cropped = page.crop((x0, top, x1, bottom))
                    txt = (cropped.extract_text() or "").strip()
                except Exception:
                    txt = ""
                if txt:
                    out[f["canonical_key"]] = txt
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# Native fill (PDF already has AcroForm widgets)
# --------------------------------------------------------------------------- #

def fill_existing_acroform(
    source_pdf: str | Path,
    fields: list[dict] | None,
    user_data: dict,
    output_pdf: str | Path,
) -> dict:
    """Set /V on the existing widgets and write to output_pdf.

    `fields` is optional — when None, we fall back to the PDF's own field
    names from PdfReader.get_fields(). When supplied, we trust the
    canonical_key → value mapping in user_data, and look up acroform_name
    on each field record (matching form_filler._fill_acroform_native's
    behavior).
    """
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter(clone_from=reader)

    updates_by_page: dict[int, dict] = {}
    filled: list[str] = []

    if fields:
        # Use detector-provided records (preserves canonical_key mapping).
        for f in fields:
            if f.get("strategy") != "acroform":
                continue
            key = f["canonical_key"]
            if key not in user_data:
                continue
            value = user_data[key]
            if f.get("field_type") == "checkbox":
                value = "/Yes" if _is_truthy(value) else "/Off"
            page_idx = f["page"] - 1
            updates_by_page.setdefault(page_idx, {})[f["acroform_name"]] = str(value)
            filled.append(key)
    else:
        # No detector input — match by acroform_name == canonical_key.
        pdf_fields = reader.get_fields() or {}
        for name, field in pdf_fields.items():
            if name not in user_data:
                continue
            value = user_data[name]
            if field.get("/FT") == "/Btn":
                value = "/Yes" if _is_truthy(value) else "/Off"
            # Find which page this field is on.
            for page_idx, page in enumerate(reader.pages):
                annots = page.get("/Annots") or []
                for annot_ref in annots:
                    annot = annot_ref.get_object()
                    if annot.get("/T") == name:
                        updates_by_page.setdefault(page_idx, {})[name] = str(value)
                        filled.append(name)
                        break
                else:
                    continue
                break

    for page_idx, updates in updates_by_page.items():
        writer.update_page_form_field_values(writer.pages[page_idx], updates)

    if "/AcroForm" in writer._root_object:
        writer._root_object["/AcroForm"][NameObject("/NeedAppearances")] = \
            BooleanObject(True)

    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    with open(output_pdf, "wb") as fh:
        writer.write(fh)

    return {
        "num_fields": count_acroform_fields(source_pdf),
        "num_filled": len(filled),
        "num_carried_over": 0,
        "filled": filled,
    }


# --------------------------------------------------------------------------- #
# Widget injection (PDF has no native form fields)
# --------------------------------------------------------------------------- #

def _bbox_to_pdf_rect(bbox: tuple, page_height: float) -> ArrayObject:
    """pdfplumber top-left bbox → PDF bottom-left /Rect."""
    x0, top, x1, bottom = bbox
    rl_y0 = page_height - float(bottom)
    rl_y1 = page_height - float(top)
    return ArrayObject([
        FloatObject(float(x0)),
        FloatObject(rl_y0),
        FloatObject(float(x1)),
        FloatObject(rl_y1),
    ])


def _make_text_widget(name: str, rect: ArrayObject, value: str) -> DictionaryObject:
    annot = DictionaryObject()
    annot[NameObject("/Type")] = NameObject("/Annot")
    annot[NameObject("/Subtype")] = NameObject("/Widget")
    annot[NameObject("/FT")] = NameObject("/Tx")
    annot[NameObject("/Rect")] = rect
    annot[NameObject("/T")] = TextStringObject(name)
    if value:
        annot[NameObject("/V")] = TextStringObject(value)
        annot[NameObject("/DV")] = TextStringObject(value)
    annot[NameObject("/DA")] = TextStringObject("/Helv 0 Tf 0 g")
    annot[NameObject("/F")] = NumberObject(4)        # printable
    return annot


def _make_button_widget(
    name: str, rect: ArrayObject, *, value: str, is_radio: bool
) -> DictionaryObject:
    annot = DictionaryObject()
    annot[NameObject("/Type")] = NameObject("/Annot")
    annot[NameObject("/Subtype")] = NameObject("/Widget")
    annot[NameObject("/FT")] = NameObject("/Btn")
    annot[NameObject("/Rect")] = rect
    annot[NameObject("/T")] = TextStringObject(name)
    annot[NameObject("/V")] = NameObject(value)       # /Yes or /Off
    annot[NameObject("/AS")] = NameObject(value)
    if is_radio:
        annot[NameObject("/Ff")] = NumberObject(32768)  # radio bit
    annot[NameObject("/F")] = NumberObject(4)
    return annot


def inject_acroform_widgets(
    source_pdf: str | Path,
    fields: list[dict],
    user_data: dict | None,
    carry_over: dict | None,
    output_pdf: str | Path,
) -> dict:
    """Add AcroForm widgets at each detected bbox, optionally pre-populated.

    Per-field /V precedence: user_data > carry_over > empty.
    """
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter(clone_from=reader)

    user_data = user_data or {}
    carry_over = carry_over or {}

    # Get / create /AcroForm dict on the catalog.
    catalog = writer._root_object
    if "/AcroForm" not in catalog:
        af = DictionaryObject()
        af[NameObject("/Fields")] = ArrayObject()
        catalog[NameObject("/AcroForm")] = af
    acroform = catalog["/AcroForm"]
    acroform[NameObject("/NeedAppearances")] = BooleanObject(True)
    if "/Fields" not in acroform:
        acroform[NameObject("/Fields")] = ArrayObject()
    fields_array: ArrayObject = acroform["/Fields"]

    # Group detected fields by page so we touch each page's /Annots once.
    by_page: dict[int, list[dict]] = {}
    for f in fields:
        if f.get("strategy") == "acroform":
            continue  # already a widget; leave it alone
        by_page.setdefault(f["page"] - 1, []).append(f)

    seen_names: set[str] = set()
    n_filled = 0
    n_carried = 0

    for page_idx, page_fields in by_page.items():
        page = writer.pages[page_idx]
        page_h = float(page.mediabox.height)

        if "/Annots" not in page:
            page[NameObject("/Annots")] = ArrayObject()
        annots: ArrayObject = page["/Annots"]

        for f in page_fields:
            base = f["canonical_key"]
            # /T must be unique within the document.
            name = base
            if name in seen_names:
                i = 2
                while f"{base}_{i}" in seen_names:
                    i += 1
                name = f"{base}_{i}"
            seen_names.add(name)

            rect = _bbox_to_pdf_rect(f["bbox"], page_h)
            ftype = f.get("field_type", "text")

            value_source = None
            if base in user_data:
                value_source = "user"
                value_raw = user_data[base]
            elif base in carry_over:
                value_source = "carry"
                value_raw = carry_over[base]
            else:
                value_raw = None

            if ftype == "checkbox":
                if value_raw is None:
                    btn_value = "/Off"
                else:
                    btn_value = "/Yes" if _is_truthy(value_raw) else "/Off"
                annot = _make_button_widget(name, rect,
                                            value=btn_value, is_radio=False)
            elif ftype == "radio":
                btn_value = "/Yes" if _is_truthy(value_raw) else "/Off"
                annot = _make_button_widget(name, rect,
                                            value=btn_value, is_radio=True)
            else:  # text, signature, unknown
                str_value = "" if value_raw is None else str(value_raw)
                annot = _make_text_widget(name, rect, str_value)

            if value_source == "user" and (
                ftype != "checkbox" or btn_value == "/Yes"
            ):
                n_filled += 1
            elif value_source == "carry":
                n_carried += 1

            # Add to the writer as an indirect object, then to /Annots and /Fields.
            ref = writer._add_object(annot)
            annot[NameObject("/P")] = page.indirect_reference
            annots.append(ref)
            fields_array.append(ref)

    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    with open(output_pdf, "wb") as fh:
        writer.write(fh)

    return {
        "num_fields": len(seen_names),
        "num_filled": n_filled,
        "num_carried_over": n_carried,
    }
