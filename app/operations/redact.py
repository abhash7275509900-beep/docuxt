"""
Redaction — Phase 5.

True redaction works at page granularity:
  1. Validate all regions up front (page must be int 1..N, coordinates finite, width/height > 0).
  2. Rasterize each affected page to an image at high DPI using pdfium's displayed geometry
     (which respects /Rotate and CropBox, matching OCR and PII detection coordinates).
  3. Burn padded black boxes into the image pixels at the requested regions.
  4. Replace the entire affected page with a new page built from nothing but that image,
     and strip document metadata/annotations that could retain sensitive content.
"""
import io
import math
import os
import tempfile
from PIL import ImageDraw
import pypdfium2 as pdfium
import img2pdf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

from app import storage

RASTER_DPI = 300
PAD_PT = 1.5


def _validate_regions(regions, n: int):
    if not isinstance(regions, list) or not regions:
        raise ValueError("regions must be a non-empty list")
    out = []
    for r in regions:
        if not isinstance(r, dict):
            raise ValueError("each region must be an object")
        p = r.get("page")
        if isinstance(p, bool) or not isinstance(p, int) or not (1 <= p <= n):
            raise ValueError(f"region page {p!r} must be an integer 1..{n}")
        vals = {}
        for k in ("x", "y", "width", "height"):
            v = r.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"region {k} must be a finite number")
            vals[k] = float(v)
        if vals["width"] <= 0 or vals["height"] <= 0:
            raise ValueError("region width/height must be > 0")
        out.append({"page": p, **vals})
    return out


def redact(file_id, regions: list):
    """regions: [{page, x, y, width, height}, ...] — coordinates in
    displayed PDF points, bottom-left origin (same convention as the
    annotation system, OCR word_boxes, and detect_pii)."""
    src_path = storage.resolve(file_id)
    reader = PdfReader(src_path)
    n_pages = len(reader.pages)
    clean_regions = _validate_regions(regions, n_pages)

    by_page = {}
    for r in clean_regions:
        by_page.setdefault(r["page"], []).append(r)

    pdfium_doc = pdfium.PdfDocument(src_path)
    writer = PdfWriter()
    pages_redacted = []
    regions_applied = 0

    try:
        for idx, page in enumerate(reader.pages):
            page_num = idx + 1
            page_regions = by_page.get(page_num)
            if not page_regions:
                writer.add_page(page)
                continue

            pdfium_page = pdfium_doc[idx]
            w_pt, h_pt = pdfium_page.get_size()  # rotation and CropBox applied
            scale = RASTER_DPI / 72.0
            bitmap = pdfium_page.render(scale=scale)
            pil_image = bitmap.to_pil().convert("RGB")

            draw = ImageDraw.Draw(pil_image)
            img_w, img_h = float(pil_image.width), float(pil_image.height)
            for r in page_regions:
                x0 = max(0.0, (r["x"] - PAD_PT) * scale)
                y0_top = max(0.0, (h_pt - r["y"] - r["height"] - PAD_PT) * scale)
                x1 = min(img_w, (r["x"] + r["width"] + PAD_PT) * scale)
                y1_top = min(img_h, (h_pt - r["y"] + PAD_PT) * scale)
                if x1 > x0 and y1_top > y0_top:
                    draw.rectangle([x0, y0_top, x1, y1_top], fill="black")
                regions_applied += 1

            fd_img, tmp_img_path = tempfile.mkstemp(suffix=".png")
            os.close(fd_img)
            try:
                pil_image.save(tmp_img_path, dpi=(RASTER_DPI, RASTER_DPI))
                layout_fn = img2pdf.get_layout_fun((w_pt, h_pt))
                page_pdf_bytes = img2pdf.convert(tmp_img_path, layout_fun=layout_fn)
            finally:
                if os.path.exists(tmp_img_path):
                    os.remove(tmp_img_path)

            flattened_page = PdfReader(io.BytesIO(page_pdf_bytes)).pages[0]
            writer.add_page(flattened_page)
            pages_redacted.append(page_num)
    finally:
        if hasattr(pdfium_doc, "close"):
            try:
                pdfium_doc.close()
            except Exception:
                pass

    # Strip document metadata/attachments that could still hold secrets
    if writer._info is not None:
        try:
            writer._info.get_object().clear()
        except Exception:
            pass
    for key in ("/Names", "/EmbeddedFiles"):
        if key in writer._root_object:
            del writer._root_object[NameObject(key)]

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    out_id = storage.save_output_path(out_path, ".pdf")

    return {
        "file_id": out_id,
        "pages_redacted": pages_redacted,
        "regions_applied": regions_applied,
    }
