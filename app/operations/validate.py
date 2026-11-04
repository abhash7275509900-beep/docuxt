"""
Format validation — Phase 3, Part B.

Runs synchronously at upload time so a corrupt, mislabeled, or unsupported
file is rejected with a clear 400 immediately. Encrypted PDFs are accepted
as valid PDFs so they can be inspected or decrypted via /pdf/decrypt.
Guards against Pillow decompression bombs and never leaks server file paths.
"""
import os
import zipfile
import pikepdf
from docx import Document as DocxDocument
from openpyxl import load_workbook
from pptx import Presentation
from PIL import Image

# Guard against decompression bombs
Image.MAX_IMAGE_PIXELS = 40_000_000

VALIDATORS = {}

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx", ".xlsx", ".pptx",
    ".doc", ".xls", ".ppt",
    ".odt", ".ods", ".odp", ".rtf",
    ".txt", ".csv", ".md",
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".html", ".xyz",
}

_BINARY_MAGIC_PREFIXES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\xd0\xcf\x11\xe0",
    b"\x7fELF",
    b"MZ",
)


def _validator(*extensions):
    def deco(fn):
        for ext in extensions:
            VALIDATORS[ext] = fn
        return fn
    return deco


@_validator(".pdf")
def _validate_pdf(path):
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise ValueError("missing PDF header")
    try:
        with pikepdf.open(path) as pdf:
            _ = len(pdf.pages)
    except pikepdf.PasswordError:
        return "pdf"  # valid PDF, password-encrypted
    return "pdf"


@_validator(".docx")
def _validate_docx(path):
    doc = DocxDocument(path)
    _ = len(doc.paragraphs)
    return "docx"


@_validator(".xlsx")
def _validate_xlsx(path):
    wb = load_workbook(path, read_only=True)
    _ = wb.sheetnames
    wb.close()
    return "xlsx"


@_validator(".pptx")
def _validate_pptx(path):
    prs = Presentation(path)
    _ = len(prs.slides)
    return "pptx"


@_validator(".odt", ".ods", ".odp")
def _validate_odf(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "content.xml" not in names and "mimetype" not in names:
            raise ValueError("not a valid OpenDocument archive")
    return ext


@_validator(".doc", ".xls", ".ppt")
def _validate_legacy_office(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    with open(path, "rb") as f:
        head = f.read(16)
    # OLE2 Compound Document magic bytes or RTF/ZIP fallback
    if not (
        head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        or head.startswith(b"{\\rtf")
        or head.startswith(b"PK\x03\x04")
    ):
        raise ValueError("not a valid legacy Office file")
    return ext


@_validator(".rtf")
def _validate_rtf(path):
    with open(path, "rb") as f:
        head = f.read(16)
    if not head.startswith(b"{\\rtf"):
        raise ValueError("not a valid RTF file")
    return "rtf"


@_validator(".txt", ".csv", ".md")
def _validate_text(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    with open(path, "rb") as f:
        sample = f.read(65536)
    if b"\x00" in sample:
        raise ValueError("binary content in text file")
    sample.decode("utf-8")
    return ext


@_validator(".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".webp", ".bmp", ".tiff", ".tif")
def _validate_image(path):
    with Image.open(path) as img:
        img.verify()
    return "image"


def validate(path, filename):
    """Returns (format, error). Never leaks internal filesystem paths."""
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext or ext not in ALLOWED_EXTENSIONS:
        return None, f"unsupported or missing file extension: {ext or '(none)'}"

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None, f"corrupt or empty {ext} file"

    validator = VALIDATORS.get(ext)
    if validator is None:
        # Allowed pass-through text extensions (.xyz, .html) must not contain binary/spoofed magic
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            if b"\x00" in head or any(head.startswith(m) for m in _BINARY_MAGIC_PREFIXES):
                return None, f"corrupt or not a valid {ext} file"
        except Exception:
            return None, f"corrupt or not a valid {ext} file"
        return "unknown", None

    try:
        fmt = validator(path)
        return fmt, None
    except Image.DecompressionBombError:
        return None, "image dimensions exceed safety limits"
    except Exception:
        return None, f"corrupt or not a valid {ext} file"
