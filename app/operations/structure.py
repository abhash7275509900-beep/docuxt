"""
Bookmarks (outline/table of contents) and hyperlink annotations.
Preserves document metadata, existing bookmarks, and AcroForm fields,
and transforms link coordinates between displayed space and user space.
"""
import os
import tempfile
import urllib.parse
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import Fit

from app import storage
from app.operations import (
    parse_finite_float,
    copy_metadata_and_forms,
    displayed_to_user_rect,
    user_to_displayed_rect,
)

ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def _find_top_level_outline_item(writer, title):
    """Walks the writer's real outline tree (/First -> /Next chain) to
    find a top-level item by title, returning the actual TreeObject
    node so it can be passed as `parent` to add_outline_item."""
    root = writer.get_outline_root()
    if "/First" not in root:
        return None
    node = root["/First"]
    while node is not None:
        if node.get("/Title") == title:
            return node
        node = node.get("/Next")
    return None


def add_bookmark(file_id, title: str, page_number: int, parent_title: str = None):
    """page_number is 1-indexed. If parent_title is given and matches
    an existing TOP-LEVEL bookmark, the new bookmark nests under it."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("bookmark title must be a non-empty string")
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise ValueError("page must be an integer")
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    n = len(reader.pages)
    if page_number < 1 or page_number > n:
        raise ValueError(f"page_number {page_number} out of range (1..{n})")

    writer = PdfWriter()
    writer.append(reader)

    parent = _find_top_level_outline_item(writer, parent_title) if parent_title else None
    if parent_title and parent is None:
        raise ValueError(f"no top-level bookmark titled {parent_title!r} to nest under")

    writer.add_outline_item(title, page_number - 1, parent=parent)
    copy_metadata_and_forms(reader, writer)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(out_path, ".pdf")


def _flatten_outline(items, depth=0):
    flat = []
    for item in items:
        if isinstance(item, list):
            flat.extend(_flatten_outline(item, depth + 1))
        else:
            flat.append({"title": str(item.title), "depth": depth})
    return flat


def list_bookmarks(file_id):
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    try:
        outline = reader.outline
    except Exception:
        outline = []
    return _flatten_outline(outline)


def add_hyperlink(file_id, page_number: int, x: float, y: float, width: float,
                   height: float, url: str = None, target_page: int = None):
    """Either url (external link) or target_page (1-indexed, internal
    link to another page) must be given, not both. Coordinates: PDF
    points, bottom-left origin in displayed page space."""
    if (url is not None and url != "") == (target_page is not None):
        raise ValueError("provide exactly one of url or target_page")

    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise ValueError("page must be an integer")
    x = parse_finite_float(x, "x")
    y = parse_finite_float(y, "y")
    width = parse_finite_float(width, "width", min_val=0.0, exclusive_min=True)
    height = parse_finite_float(height, "height", min_val=0.0, exclusive_min=True)

    src = storage.resolve(file_id)
    reader = PdfReader(src)
    n = len(reader.pages)
    if page_number < 1 or page_number > n:
        raise ValueError(f"page_number {page_number} out of range (1..{n})")

    if url:
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
            raise ValueError(f"url scheme must be one of {sorted(ALLOWED_URL_SCHEMES)}")
        url = url.strip()
    else:
        if isinstance(target_page, bool) or not isinstance(target_page, int):
            raise ValueError("target_page must be an integer")
        if target_page < 1 or target_page > n:
            raise ValueError(f"target_page {target_page} out of range (1..{n})")

    writer = PdfWriter(clone_from=src)
    target_page_obj = writer.pages[page_number - 1]
    rect = displayed_to_user_rect(target_page_obj, x, y, width, height)

    if url:
        annotation = Link(rect=rect, url=url)
    else:
        annotation = Link(rect=rect, target_page_index=target_page - 1, fit=Fit.fit())

    writer.add_annotation(page_number=page_number - 1, annotation=annotation)
    copy_metadata_and_forms(reader, writer)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(out_path, ".pdf")


def list_hyperlinks(file_id):
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    links = []
    for page_num, page in enumerate(reader.pages, start=1):
        for annot in page.get("/Annots", []):
            obj = annot.get_object()
            if obj.get("/Subtype") != "/Link":
                continue
            raw_rect = [float(v) for v in obj["/Rect"]]
            disp_rect = list(user_to_displayed_rect(page, raw_rect[0], raw_rect[1], raw_rect[2], raw_rect[3]))
            entry = {"page": page_num, "rect": disp_rect}
            action = obj.get("/A")
            if action:
                action_obj = action.get_object() if hasattr(action, "get_object") else action
                if action_obj.get("/S") == "/URI":
                    entry["url"] = str(action_obj.get("/URI"))
                elif action_obj.get("/S") == "/GoTo":
                    entry["internal_dest"] = True
            elif "/Dest" in obj:
                entry["internal_dest"] = True
            links.append(entry)
    return links
