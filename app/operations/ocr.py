"""
OCR — Phase 4.

For each page: if it already has a real text layer, leave it alone unless force=True.
If not (a scanned/image-only page), rasterize it and run Tesseract.
Validates dpi and force strictly and preserves document metadata and bookmarks.
"""
import io
import os
import re
import tempfile
import pypdfium2 as pdfium
import pytesseract
from pytesseract import Output
from pypdf import PdfReader, PdfWriter

from app import storage
from app.operations import (
    parse_bool,
    parse_dpi,
    copy_metadata_and_forms,
    copy_outline_to_writer,
)

MIN_TEXT_LENGTH_TO_SKIP = 20
DEFAULT_DPI = 300
_LANG_RE = re.compile(r"^[a-zA-Z0-9_+]{2,32}$")


def _page_needs_ocr(page):
    text = (page.extract_text() or "").strip()
    return len(text) < MIN_TEXT_LENGTH_TO_SKIP


def _rasterize_page(pdfium_doc, index, dpi):
    page = pdfium_doc[index]
    scale = dpi / 72.0
    bitmap = page.render(scale=scale)
    return bitmap.to_pil()


def _detect_script(pil_image):
    try:
        osd = pytesseract.image_to_osd(pil_image, output_type=Output.DICT)
        return {
            "script": osd.get("script"),
            "script_confidence": osd.get("script_conf"),
            "rotate": osd.get("rotate"),
            "orientation_confidence": osd.get("orientation_conf"),
        }
    except pytesseract.TesseractError as e:
        return {"error": str(e)}


def _word_boxes(pil_image, dpi, lang):
    data = pytesseract.image_to_data(pil_image, lang=lang, output_type=Output.DICT)
    scale = 72.0 / dpi
    img_h_pt = pil_image.height * scale
    boxes = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf_val = float(data["conf"][i])
        except (ValueError, TypeError):
            continue
        if conf_val < 0:
            continue
        left, top = data["left"][i], data["top"][i]
        width, height = data["width"][i], data["height"][i]
        x_pt = left * scale
        w_pt = width * scale
        h_pt = height * scale
        y_top_pt = top * scale
        y_pt = img_h_pt - y_top_pt - h_pt
        boxes.append({
            "text": text,
            "x": round(x_pt, 2),
            "y": round(y_pt, 2),
            "width": round(w_pt, 2),
            "height": round(h_pt, 2),
            "confidence": conf_val,
        })
    return boxes


def ocr(file_id, lang: str = "eng", dpi: int = DEFAULT_DPI, force: bool = False):
    if not isinstance(lang, str) or not _LANG_RE.match(lang):
        raise ValueError("invalid OCR language code")
    dpi = parse_dpi(dpi, default=DEFAULT_DPI, min_dpi=36, max_dpi=600)
    force = parse_bool(force, "force")

    src_path = storage.resolve(file_id)
    reader = PdfReader(src_path)
    pdfium_doc = pdfium.PdfDocument(src_path)

    writer = PdfWriter()
    pages_ocred = []
    word_boxes_by_page = {}
    script_detection = None

    try:
        for idx, page in enumerate(reader.pages):
            page_num = idx + 1
            if not force and not _page_needs_ocr(page):
                writer.add_page(page)
                continue

            pil_image = _rasterize_page(pdfium_doc, idx, dpi)

            fd_img, tmp_img_path = tempfile.mkstemp(suffix=".png")
            os.close(fd_img)
            try:
                pil_image.save(tmp_img_path, dpi=(dpi, dpi))
                pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                    tmp_img_path, lang=lang, extension="pdf"
                )
            finally:
                if os.path.exists(tmp_img_path):
                    os.remove(tmp_img_path)

            ocred_page = PdfReader(io.BytesIO(pdf_bytes)).pages[0]
            writer.add_page(ocred_page)
            pages_ocred.append(page_num)
            word_boxes_by_page[page_num] = _word_boxes(pil_image, dpi, lang)

            if script_detection is None:
                script_detection = _detect_script(pil_image)
    finally:
        if hasattr(pdfium_doc, "close"):
            try:
                pdfium_doc.close()
            except Exception:
                pass

    copy_outline_to_writer(reader, writer)
    copy_metadata_and_forms(reader, writer, copy_forms=False)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    out_id = storage.save_output_path(out_path, ".pdf")

    return {
        "file_id": out_id,
        "total_pages": len(reader.pages),
        "pages_ocred": pages_ocred,
        "word_boxes": word_boxes_by_page,
        "script_detection": script_detection,
    }
