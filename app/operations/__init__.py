"""
Shared validation, Unicode/CJK font selection, rotation/CropBox coordinate
transformation, and PDF structure preservation helpers used across operations.
"""
import math
import os
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

_FONTS_REGISTERED = False
_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def ensure_fonts_registered():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    try:
        if os.path.exists(_DEJAVU_REGULAR):
            pdfmetrics.registerFont(TTFont("DejaVuSans", _DEJAVU_REGULAR))
        if os.path.exists(_DEJAVU_BOLD):
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", _DEJAVU_BOLD))
    except Exception:
        pass
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:
        pass
    _FONTS_REGISTERED = True


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x2FFF
        or 0x3000 <= cp <= 0x303F
        or 0x3040 <= cp <= 0x30FF
        or 0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xAC00 <= cp <= 0xD7AF
        or 0xF900 <= cp <= 0xFAFF
        or 0xFF00 <= cp <= 0xFFEF
    )


def select_pdf_font(text: str, bold: bool = False) -> str:
    """Return a registered ReportLab font name capable of rendering `text`.
    Raises ValueError if `text` contains unsupported characters (e.g. supplementary
    plane emoji or control characters)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    ensure_fonts_registered()

    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            raise ValueError(f"unsupported character U+{cp:04X} in PDF text")

    try:
        text.encode("latin-1")
        return "Helvetica-Bold" if bold else "Helvetica"
    except UnicodeEncodeError:
        pass

    if any(_is_cjk_char(ch) for ch in text):
        return "STSong-Light"

    if os.path.exists(_DEJAVU_REGULAR):
        return "DejaVuSans-Bold" if (bold and os.path.exists(_DEJAVU_BOLD)) else "DejaVuSans"

    raise ValueError("text contains non-Latin characters and no Unicode TTF font is available")


def parse_bool(val, name: str = "flag") -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        if val in (0, 1):
            return bool(val)
        raise ValueError(f"{name} must be a boolean")
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "1", "yes", "on", "/yes"):
            return True
        if s in ("false", "0", "no", "off", "/off", ""):
            return False
    raise ValueError(f"{name} must be a boolean")


def parse_pages(pages, total_pages: int = None, allow_none: bool = True, allow_empty: bool = False):
    """Validate a 1-indexed list of integer page numbers."""
    if pages is None:
        if allow_none:
            return None
        raise ValueError("pages is required")
    if not isinstance(pages, list):
        raise ValueError("pages must be a list of 1-indexed integers")
    if len(pages) == 0 and not allow_empty:
        raise ValueError("pages must not be empty")
    out = []
    for p in pages:
        if isinstance(p, bool) or not isinstance(p, int):
            raise ValueError(f"page number {p!r} must be an integer")
        if p < 1:
            raise ValueError(f"page number {p} must be >= 1")
        if total_pages is not None and p > total_pages:
            raise ValueError(f"page number {p} out of range (1..{total_pages})")
        out.append(p)
    return out


def parse_dpi(dpi, default: int = 150, min_dpi: int = 36, max_dpi: int = 600) -> int:
    if dpi is None:
        return default
    if isinstance(dpi, bool) or not isinstance(dpi, int):
        raise ValueError("dpi must be an integer")
    if not (min_dpi <= dpi <= max_dpi):
        raise ValueError(f"dpi must be between {min_dpi} and {max_dpi}")
    return dpi


def parse_finite_float(val, name: str, min_val: float = None, max_val: float = None, exclusive_min: bool = False) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a number")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number")
    if min_val is not None:
        if exclusive_min and f <= min_val:
            raise ValueError(f"{name} must be > {min_val}")
        if not exclusive_min and f < min_val:
            raise ValueError(f"{name} must be >= {min_val}")
    if max_val is not None and f > max_val:
        raise ValueError(f"{name} must be <= {max_val}")
    return f


def get_displayed_page_size(page) -> tuple:
    """Return (displayed_width, displayed_height, rotation_degrees) for a pypdf PageObject."""
    crop = page.cropbox
    w = float(crop.width)
    h = float(crop.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    if rot in (90, 270):
        w, h = h, w
    return w, h, rot


def unrotated_to_displayed_rect(rot: int, ux0: float, uy0: float, ux1: float, uy1: float, W: float, H: float) -> tuple:
    """Transform a cropbox-relative unrotated rect (ux0, uy0, ux1, uy1) on a page
    of unrotated crop size (W, H) into displayed space (dx0, dy0, dx1, dy1)."""
    rot = int(rot or 0) % 360
    if rot == 90:
        return (uy0, W - ux1, uy1, W - ux0)
    if rot == 180:
        return (W - ux1, H - uy1, W - ux0, H - uy0)
    if rot == 270:
        return (H - uy1, ux0, H - uy0, ux1)
    return (ux0, uy0, ux1, uy1)


def displayed_to_user_rect(page, x: float, y: float, width: float, height: float) -> tuple:
    """Convert a rectangle (x, y, width, height) in displayed page coordinates
    (bottom-left origin, after /Rotate and /CropBox) into raw PDF user-space
    (ux0, uy0, ux1, uy1) for `page`."""
    crop = page.cropbox
    cl, cb = float(crop.left), float(crop.bottom)
    W, H = float(crop.width), float(crop.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    dx0, dy0 = float(x), float(y)
    dx1, dy1 = dx0 + float(width), dy0 + float(height)
    if rot == 90:
        ux0, uy0, ux1, uy1 = W - dy1, dx0, W - dy0, dx1
    elif rot == 180:
        ux0, uy0, ux1, uy1 = W - dx1, H - dy1, W - dx0, H - dy0
    elif rot == 270:
        ux0, uy0, ux1, uy1 = dy0, H - dx1, dy1, H - dx0
    else:
        ux0, uy0, ux1, uy1 = dx0, dy0, dx1, dy1
    return (ux0 + cl, uy0 + cb, ux1 + cl, uy1 + cb)


def user_to_displayed_rect(page, x0: float, y0: float, x1: float, y1: float) -> tuple:
    """Convert a raw PDF user-space rect (x0, y0, x1, y1) into displayed page
    coordinates (dx0, dy0, dx1, dy1)."""
    crop = page.cropbox
    cl, cb = float(crop.left), float(crop.bottom)
    W, H = float(crop.width), float(crop.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    ux0, uy0 = float(x0) - cl, float(y0) - cb
    ux1, uy1 = float(x1) - cl, float(y1) - cb
    return unrotated_to_displayed_rect(rot, ux0, uy0, ux1, uy1, W, H)


def displayed_to_user_point(page, x: float, y: float) -> tuple:
    """Convert a point (x, y) in displayed page coordinates into raw PDF user space."""
    crop = page.cropbox
    cl, cb = float(crop.left), float(crop.bottom)
    W, H = float(crop.width), float(crop.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    dx, dy = float(x), float(y)
    if rot == 90:
        ux, uy = W - dy, dx
    elif rot == 180:
        ux, uy = W - dx, H - dy
    elif rot == 270:
        ux, uy = dy, H - dx
    else:
        ux, uy = dx, dy
    return (ux + cl, uy + cb)


def copy_outline_to_writer(reader: PdfReader, writer: PdfWriter):
    """Copy top-level and nested bookmarks from `reader` onto `writer` when
    pages are rebuilt (e.g. during OCR)."""
    try:
        outline = reader.outline
    except Exception:
        return
    if not outline:
        return
    n_pages = len(writer.pages)

    def _add_items(items, parent=None):
        last_added = None
        for item in items:
            if isinstance(item, list):
                if last_added is not None:
                    _add_items(item, parent=last_added)
            else:
                try:
                    pnum = reader.get_destination_page_number(item)
                except Exception:
                    pnum = 0
                if pnum is None or pnum < 0:
                    pnum = 0
                if pnum >= n_pages:
                    pnum = max(0, n_pages - 1)
                title = str(getattr(item, "title", "") or "Bookmark")
                last_added = writer.add_outline_item(title, pnum, parent=parent)

    try:
        _add_items(outline, parent=None)
    except Exception:
        pass


def copy_metadata_and_forms(reader: PdfReader, writer: PdfWriter, copy_forms: bool = True):
    """Copy document metadata (/Info) and /AcroForm from `reader` onto `writer`."""
    try:
        if reader.metadata:
            clean_meta = {
                str(k): str(v)
                for k, v in reader.metadata.items()
                if v is not None and str(k).startswith("/")
            }
            if clean_meta:
                writer.add_metadata(clean_meta)
    except Exception:
        pass

    if copy_forms:
        try:
            root = reader.trailer.get("/Root")
            if root is not None:
                root_obj = root.get_object()
                if "/AcroForm" in root_obj and "/AcroForm" not in writer._root_object:
                    acro = root_obj["/AcroForm"].get_object()
                    writer._root_object[NameObject("/AcroForm")] = acro.clone(writer)
        except Exception:
            pass
    try:
        widget_refs = []
        for page in writer.pages:
            if "/Annots" in page:
                for annot_ref in page["/Annots"]:
                    obj = annot_ref.get_object()
                    if obj.get("/Subtype") == "/Widget":
                        widget_refs.append(annot_ref)
        if widget_refs:
            # Collapse widgets to their top-level fields so parent/kid
            # hierarchies (radio groups, dotted names) survive.
            top_refs, seen = [], set()
            for ref in widget_refs:
                node, node_ref, depth = ref.get_object(), ref, 0
                while "/Parent" in node and depth < 50:
                    node_ref = node.raw_get("/Parent")
                    node = node_ref.get_object()
                    depth += 1
                key = getattr(node_ref, "idnum", id(node))
                if key not in seen:
                    seen.add(key)
                    top_refs.append(node_ref)
            widget_refs = top_refs
            if "/AcroForm" not in writer._root_object:
                writer._root_object[NameObject("/AcroForm")] = writer._add_object(
                    DictionaryObject({
                        NameObject("/Fields"): ArrayObject(widget_refs),
                        NameObject("/DA"): TextStringObject("/Helv 10 Tf 0 g"),
                    })
                )
            else:
                acro_obj = writer._root_object["/AcroForm"].get_object()
                acro_obj[NameObject("/Fields")] = ArrayObject(widget_refs)
    except Exception:
        pass
