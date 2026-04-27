"""
Utilities for new forms:

  1. generate_template: create an empty user_data.json scaffold from a
     fields_normalized.json — every canonical_key mapped to "".
  2. visualize_fields: render the PDF with detected bboxes drawn on top,
     so you can verify detection quality before filling.

Usage:
    python3 form_utils.py template fields_normalized.json -o user_data_template.json
    python3 form_utils.py visualize source.pdf fields.json -o debug.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def generate_template(fields_json: str | Path,
                      out_path: str | Path) -> dict:
    data = json.loads(Path(fields_json).read_text())
    template: dict = {}
    comments: dict = {}
    for f in data["fields"]:
        key = f["canonical_key"]
        ftype = f.get("field_type", "text")
        if ftype == "checkbox" or ftype == "radio":
            template[key] = False
        else:
            template[key] = ""
        comments[key] = {
            "label": f.get("label", ""),
            "section": f.get("section", ""),
            "type": ftype,
            "page": f.get("page", 1),
            "strategy": f.get("strategy", ""),
        }

    out = {
        "_schema": {
            "description": "Fill in values for each canonical_key below. "
                           "Delete the _schema and _field_info keys before "
                           "passing to form_filler.py.",
            "total_fields": len(template),
        },
        "_field_info": comments,
        **template,
    }
    Path(out_path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


def visualize_fields(pdf_path: str | Path, fields_json: str | Path,
                     out_path: str | Path, dpi: int = 150) -> None:
    """Render each page with field bboxes drawn as red rectangles."""
    try:
        from pdf2image import convert_from_path
        from PIL import ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError(
            "visualize requires pdf2image and Pillow. Install with: "
            "pip install pdf2image Pillow"
        )

    data = json.loads(Path(fields_json).read_text())
    fields = data["fields"]

    images = convert_from_path(str(pdf_path), dpi=dpi)

    pages_out = []
    for idx, img in enumerate(images, start=1):
        draw = ImageDraw.Draw(img)
        page_fields = [f for f in fields if f["page"] == idx]
        if page_fields:
            # PDF points → image pixels
            scale = dpi / 72.0
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12
                )
            except Exception:
                font = ImageFont.load_default()

            color_map = {
                "acroform": (0, 128, 0),      # green
                "underscore": (200, 0, 0),    # red
                "rectangle": (0, 0, 200),     # blue
                "colon": (200, 100, 0),       # orange
                "table": (128, 0, 128),       # purple
            }
            for f in page_fields:
                x0, top, x1, bottom = [v * scale for v in f["bbox"]]
                color = color_map.get(f.get("strategy", ""), (100, 100, 100))
                draw.rectangle([x0, top, x1, bottom], outline=color, width=2)
                label = f.get("canonical_key", f.get("field_id", ""))[:25]
                draw.text((x0, max(0, top - 14)), label, fill=color, font=font)
        pages_out.append(img)

    out_path = Path(out_path)
    if len(pages_out) == 1:
        pages_out[0].save(out_path, "PNG")
    else:
        # Save multi-page as separate files
        stem = out_path.with_suffix("")
        for i, img in enumerate(pages_out, start=1):
            img.save(f"{stem}_page{i}.png", "PNG")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    tp = sub.add_parser("template", help="Generate empty user_data template")
    tp.add_argument("fields_json")
    tp.add_argument("-o", "--output", default="user_data_template.json")

    vp = sub.add_parser("visualize", help="Draw detected bboxes on PDF pages")
    vp.add_argument("pdf")
    vp.add_argument("fields_json")
    vp.add_argument("-o", "--output", default="debug.png")
    vp.add_argument("--dpi", type=int, default=150)

    args = ap.parse_args()

    if args.cmd == "template":
        data = generate_template(args.fields_json, args.output)
        total = data["_schema"]["total_fields"]
        print(f"Generated template with {total} fields → {args.output}")
    elif args.cmd == "visualize":
        visualize_fields(args.pdf, args.fields_json, args.output, args.dpi)
        print(f"Saved visualization → {args.output}")
