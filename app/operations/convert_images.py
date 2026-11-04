"""
Image <-> PDF conversion.
images_to_pdf: one or more images (jpg/png) -> single PDF
pdf_to_images: PDF -> list of image file_ids (one per page)
"""
import os
import tempfile
import img2pdf
import pypdfium2 as pdfium
from PIL import Image
from app import storage
from app.operations import parse_dpi


def images_to_pdf(file_ids: list):
    if not file_ids or not isinstance(file_ids, list):
        raise ValueError("file_ids must be a non-empty list")
    paths = [storage.resolve(fid) for fid in file_ids]
    clean_paths = []
    tmp_files = []
    try:
        for p in paths:
            with Image.open(p) as img:
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[3])
                    fd, tmp_p = tempfile.mkstemp(suffix=".png")
                    os.close(fd)
                    bg.save(tmp_p, format="PNG")
                    clean_paths.append(tmp_p)
                    tmp_files.append(tmp_p)
                else:
                    clean_paths.append(p)
        pdf_bytes = img2pdf.convert(clean_paths)
        return storage.save_output_bytes(pdf_bytes, ".pdf")
    finally:
        for tp in tmp_files:
            if os.path.exists(tp):
                try:
                    os.remove(tp)
                except OSError:
                    pass


def pdf_to_images(file_id, fmt: str = "png", dpi: int = 150):
    if not isinstance(fmt, str):
        raise ValueError("fmt must be png or jpg")
    fmt = fmt.lower()
    if fmt not in ("png", "jpg", "jpeg"):
        raise ValueError("fmt must be png or jpg")
    dpi = parse_dpi(dpi, default=150, min_dpi=36, max_dpi=600)

    src = storage.resolve(file_id)
    pdf = pdfium.PdfDocument(src)
    scale = dpi / 72.0

    out_ids = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            ext = ".png" if fmt == "png" else ".jpg"
            if ext == ".jpg" and pil_image.mode != "RGB":
                pil_image = pil_image.convert("RGB")
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            pil_image.save(tmp_path, format="PNG" if fmt == "png" else "JPEG")
            out_ids.append(storage.save_output_path(tmp_path, ext))
    finally:
        if hasattr(pdf, "close"):
            try:
                pdf.close()
            except Exception:
                pass
    return out_ids
