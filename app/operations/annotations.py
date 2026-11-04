"""
Annotation layer — Phase 2.

Annotations are stored as JSON rows in the DB (file_id, type, page,
data), validated strictly at creation/update time, and rendered as
vector overlays onto a cloned PdfWriter during export while preserving
bookmarks, AcroForm fields, and metadata.
"""
import io
import json
import os
import tempfile
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor

from app import storage, db
from app.operations import (
    parse_finite_float,
    select_pdf_font,
    copy_metadata_and_forms,
    displayed_to_user_rect,
    displayed_to_user_point,
)

ALLOWED_TYPES = {
    "highlight", "underline", "strikethrough", "freehand", "comment", "stamp",
    "text_edit",
}

REQUIRED_KEYS = {
    "highlight": ("x", "y", "width", "height"),
    "underline": ("x", "y", "width"),
    "strikethrough": ("x", "y", "width"),
    "freehand": ("points",),
    "comment": ("x", "y"),
    "stamp": ("x", "y", "text"),
    "text_edit": ("x", "y", "width", "height", "text"),
}


def _validate_hex_color(val, field_name="color"):
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{field_name} must be a hex color string")
    try:
        HexColor(val.strip())
    except Exception as e:
        raise ValueError(f"invalid {field_name}: {val!r}") from e


def _validate_annotation_payload(file_id, ann_type, page, data: dict):
    if ann_type not in ALLOWED_TYPES:
        raise ValueError(f"type must be one of {sorted(ALLOWED_TYPES)}")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a 1-indexed integer")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    src_path = storage.resolve(file_id)
    try:
        reader = PdfReader(src_path)
        n_pages = len(reader.pages)
    except Exception as e:
        raise ValueError("target file is not a readable PDF") from e
    if page > n_pages:
        raise ValueError(f"page {page} out of range (1..{n_pages})")

    for req_key in REQUIRED_KEYS[ann_type]:
        if req_key not in data or data[req_key] is None:
            raise ValueError(f"{ann_type} annotation requires {req_key!r}")

    for k in ("x", "y"):
        if k in data and data[k] is not None:
            parse_finite_float(data[k], k)

    for k in ("width", "height", "font_size", "stroke_width"):
        if k in data and data[k] is not None:
            parse_finite_float(data[k], k, min_val=0.0, exclusive_min=True)

    if "color" in data and data["color"] is not None:
        _validate_hex_color(data["color"], "color")
    if "background" in data and data["background"] is not None:
        _validate_hex_color(data["background"], "background")

    if ann_type == "freehand":
        pts = data.get("points")
        if not isinstance(pts, list) or len(pts) < 2:
            raise ValueError("freehand points must be a list of at least 2 [x, y] pairs")
        for pt in pts:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ValueError("each freehand point must be an [x, y] pair")
            parse_finite_float(pt[0], "point x")
            parse_finite_float(pt[1], "point y")

    if ann_type in ("stamp", "text_edit"):
        txt = data.get("text")
        if not isinstance(txt, str) or not txt.strip():
            raise ValueError(f"{ann_type} text must be a non-empty string")
        select_pdf_font(txt, bold=(ann_type == "stamp"))


def add_annotation(file_id, owner_id, ann_type, page, data: dict):
    _validate_annotation_payload(file_id, ann_type, page, data)
    ann_id = db.add_annotation(file_id, owner_id, ann_type, json.dumps(data), page)
    return ann_id


def update_annotation(annotation_id, owner_id, ann_type=None, page=None, data=None):
    existing = db.get_annotation(annotation_id)
    if existing is None or existing["owner_id"] != owner_id:
        return False
    new_type = ann_type if ann_type is not None else existing["type"]
    new_page = page if page is not None else existing["page"]
    new_data = data if data is not None else json.loads(existing["data"])
    _validate_annotation_payload(existing["file_id"], new_type, new_page, new_data)
    return db.update_annotation(annotation_id, owner_id, new_type, json.dumps(new_data), new_page)


def list_annotations(file_id):
    rows = db.list_annotations(file_id)
    for r in rows:
        r["data"] = json.loads(r["data"])
    return rows


def delete_annotation(annotation_id, owner_id):
    return db.delete_annotation(annotation_id, owner_id)


def export(file_id):
    """Flatten all stored annotations onto the PDF while preserving bookmarks,
    AcroForm fields, and metadata. Skips any corrupt/invalid annotation rows
    rather than aborting the export. Returns new file_id."""
    src_path = storage.resolve(file_id)
    reader = PdfReader(src_path)
    annotations = list_annotations(file_id)

    by_page = {}
    for ann in annotations:
        by_page.setdefault(ann["page"], []).append(ann)

    writer = PdfWriter(clone_from=src_path)
    for idx, page in enumerate(writer.pages, start=1):
        page_anns = by_page.get(idx)
        if page_anns:
            box = page.mediabox
            width = float(box.right)
            height = float(box.top)
            overlay_pdf = _render_overlay(page, width, height, page_anns)
            overlay_page = PdfReader(overlay_pdf).pages[0]
            page.merge_page(overlay_page)

    copy_metadata_and_forms(reader, writer)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(out_path, ".pdf")


def _render_overlay(page, width, height, anns):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))

    for ann in anns:
        try:
            data = ann["data"]
            color = HexColor(data.get("color", "#FFFF00"))
            t = ann["type"]

            if t == "highlight":
                ux0, uy0, ux1, uy1 = displayed_to_user_rect(
                    page, data["x"], data["y"], data["width"], data["height"]
                )
                c.saveState()
                c.setFillColor(color, alpha=0.4)
                c.rect(ux0, uy0, ux1 - ux0, uy1 - uy0, fill=1, stroke=0)
                c.restoreState()

            elif t in ("underline", "strikethrough"):
                ux0, uy0 = displayed_to_user_point(page, data["x"], data["y"])
                ux1, uy1 = displayed_to_user_point(page, data["x"] + data["width"], data["y"])
                c.saveState()
                c.setStrokeColor(color)
                c.setLineWidth(1.5)
                c.line(ux0, uy0, ux1, uy1)
                c.restoreState()

            elif t == "freehand":
                points = data["points"]
                if isinstance(points, list) and len(points) >= 2:
                    c.saveState()
                    c.setStrokeColor(color)
                    c.setLineWidth(float(data.get("stroke_width", 2)))
                    path = c.beginPath()
                    p0x, p0y = displayed_to_user_point(page, points[0][0], points[0][1])
                    path.moveTo(p0x, p0y)
                    for px, py in points[1:]:
                        upx, upy = displayed_to_user_point(page, px, py)
                        path.lineTo(upx, upy)
                    c.drawPath(path, stroke=1, fill=0)
                    c.restoreState()

            elif t == "comment":
                ux, uy = displayed_to_user_point(page, data["x"], data["y"])
                c.saveState()
                c.setFillColor(color)
                c.circle(ux, uy, 6, fill=1, stroke=0)
                c.restoreState()

            elif t == "stamp":
                stamp_text = str(data.get("text", ""))
                font_name = select_pdf_font(stamp_text, bold=True) if stamp_text else "Helvetica-Bold"
                ux, uy = displayed_to_user_point(page, data["x"], data["y"])
                c.saveState()
                c.setFillColor(color)
                c.setFont(font_name, 12)
                c.drawString(ux, uy, stamp_text)
                c.restoreState()

            elif t == "text_edit":
                bg = HexColor(data.get("background", "#FFFFFF"))
                ux0, uy0, ux1, uy1 = displayed_to_user_rect(
                    page, data["x"], data["y"], data["width"], data["height"]
                )
                c.saveState()
                c.setFillColor(bg)
                c.rect(ux0, uy0, ux1 - ux0, uy1 - uy0, fill=1, stroke=0)
                font_size = float(data.get("font_size", 11))
                edit_text = str(data.get("text", ""))
                font_name = select_pdf_font(edit_text, bold=False) if edit_text else "Helvetica"
                c.setFillColor(color)
                c.setFont(font_name, font_size)
                text_y = uy0 + max(0, ((uy1 - uy0) - font_size) / 2)
                c.drawString(ux0 + 1, text_y, edit_text)
                c.restoreState()
        except Exception:
            # Skip malformed/legacy row rather than failing the whole export
            continue

    c.save()
    buf.seek(0)
    return buf
