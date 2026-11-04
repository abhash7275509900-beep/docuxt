"""
Watermarking — Phase 5.

Generates a single-page transparent PDF overlay via ReportLab and merges
it onto each target page while preserving bookmarks, AcroForm fields,
and document metadata. Supports Latin, Cyrillic/Greek/Extended Latin
(DejaVuSans), and CJK (STSong-Light) text and accounts for CropBox/MediaBox origins.
"""
import io
import math
import os
import tempfile
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor

from app import storage
from app.operations import (
    parse_bool,
    parse_pages,
    parse_finite_float,
    select_pdf_font,
    copy_metadata_and_forms,
)


def _make_watermark_page(box_left, box_bottom, box_right, box_top, text, opacity, font_size, color_hex, diagonal, font_name, rot=0):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(box_right, box_top))
    c.saveState()
    try:
        color = HexColor(color_hex)
    except Exception as e:
        raise ValueError(f"invalid hex color: {color_hex!r}") from e
    c.setFillColor(color, alpha=opacity)
    c.setFont(font_name, font_size)

    cx = (box_left + box_right) / 2.0
    cy = (box_bottom + box_top) / 2.0
    disp_w, disp_h = (box_right - box_left), (box_top - box_bottom)
    if rot in (90, 270):
        disp_w, disp_h = disp_h, disp_w
    c.translate(cx, cy)
    angle = math.degrees(math.atan2(disp_h, disp_w)) if diagonal else 0.0
    c.rotate(angle + rot)
    c.drawCentredString(0, -font_size / 3, text)

    c.restoreState()
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def add_watermark(file_id, text: str, opacity: float = 0.3,
                  font_size: int = 40, color: str = "#888888",
                  diagonal: bool = True, pages: list = None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("watermark text must not be empty")
    opacity = parse_finite_float(opacity, "opacity", min_val=0.0, max_val=1.0, exclusive_min=True)
    font_size = int(parse_finite_float(font_size, "font_size", min_val=4.0, max_val=300.0))
    diagonal = parse_bool(diagonal, "diagonal")
    font_name = select_pdf_font(text, bold=True)

    src = storage.resolve(file_id)
    reader = PdfReader(src)
    n_pages = len(reader.pages)
    validated_pages = parse_pages(pages, total_pages=n_pages, allow_none=True, allow_empty=False)
    target = set(validated_pages) if validated_pages is not None else set(range(1, n_pages + 1))

    writer = PdfWriter(clone_from=src)
    cache = {}
    for idx, page in enumerate(writer.pages, start=1):
        if idx in target:
            crop = page.cropbox
            mb = page.mediabox
            rot = int(page.get("/Rotate", 0) or 0) % 360
            dims = (
                rot,
                round(float(crop.left), 2),
                round(float(crop.bottom), 2),
                round(max(float(crop.right), float(mb.right)), 2),
                round(max(float(crop.top), float(mb.top)), 2),
            )
            if dims not in cache:
                cache[dims] = _make_watermark_page(
                    dims[1], dims[2], dims[3], dims[4],
                    text, opacity, font_size, color, diagonal, font_name, rot=dims[0]
                )
            page.merge_page(cache[dims])

    copy_metadata_and_forms(reader, writer)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(out_path, ".pdf")
