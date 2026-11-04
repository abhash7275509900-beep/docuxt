"""
Page-level PDF operations, built on pypdf.
Each function: takes file_id(s) + params, returns a new file_id.
Preserves bookmarks, AcroForm fields, and document metadata across operations.
"""
import os
import tempfile
from pypdf import PdfReader, PdfWriter
from app import storage
from app.operations import parse_pages, copy_metadata_and_forms


def merge(file_ids: list):
    if not file_ids or not isinstance(file_ids, list):
        raise ValueError("file_ids must be a non-empty list")
    writer = PdfWriter()
    first_reader = None
    for fid in file_ids:
        reader = PdfReader(storage.resolve(fid))
        if first_reader is None:
            first_reader = reader
        writer.append(reader)
    if first_reader is not None:
        copy_metadata_and_forms(first_reader, writer)
    return _write(writer)


def split(file_id, ranges: list = None):
    """
    ranges: list of [start, end] (1-indexed, inclusive). If None, splits
    into one file per page.
    Returns a list of file_ids.
    """
    reader = PdfReader(storage.resolve(file_id))
    n = len(reader.pages)

    if ranges is None:
        clean_ranges = [[i, i] for i in range(1, n + 1)]
    else:
        if not isinstance(ranges, list) or len(ranges) == 0:
            raise ValueError("ranges must be a non-empty list")
        clean_ranges = []
        for r in ranges:
            if not isinstance(r, (list, tuple)) or len(r) != 2:
                raise ValueError("each range must be a [start, end] pair")
            start, end = r[0], r[1]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                raise ValueError("range bounds must be integers")
            if start < 1 or end < start or end > n:
                raise ValueError(f"invalid page range [{start}, {end}] for document with {n} pages")
            clean_ranges.append([start, end])

    out_ids = []
    for start, end in clean_ranges:
        writer = PdfWriter()
        writer.append(reader, pages=list(range(start - 1, end)))
        copy_metadata_and_forms(reader, writer)
        out_ids.append(_write(writer))
    return out_ids


def reorder(file_id, order: list):
    """order: new page order, 1-indexed, e.g. [3, 1, 2]"""
    reader = PdfReader(storage.resolve(file_id))
    n = len(reader.pages)
    clean_order = parse_pages(order, n, allow_none=False)
    writer = PdfWriter()
    writer.append(reader, pages=[p - 1 for p in clean_order])
    copy_metadata_and_forms(reader, writer)
    return _write(writer)


def rotate(file_id, pages: list = None, degrees: int = 90):
    """pages: 1-indexed list, or None for all pages"""
    if isinstance(degrees, bool) or not isinstance(degrees, int) or degrees % 90 != 0:
        raise ValueError("degrees must be an integer multiple of 90")
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    n = len(reader.pages)
    clean_pages = parse_pages(pages, n, allow_none=True)
    target = set(clean_pages) if clean_pages is not None else None

    writer = PdfWriter(clone_from=src)
    for idx, page in enumerate(writer.pages, start=1):
        if target is None or idx in target:
            page.rotate(degrees)
    copy_metadata_and_forms(reader, writer)
    return _write(writer)


def delete_pages(file_id, pages: list):
    """pages: 1-indexed list of pages to remove"""
    reader = PdfReader(storage.resolve(file_id))
    n = len(reader.pages)
    clean_pages = parse_pages(pages, n, allow_none=False)
    remove = set(clean_pages)
    if len(remove) >= n:
        raise ValueError("cannot delete all pages from the document")
    keep = [idx - 1 for idx in range(1, n + 1) if idx not in remove]
    writer = PdfWriter()
    writer.append(reader, pages=keep)
    copy_metadata_and_forms(reader, writer)
    return _write(writer)


def extract_pages(file_id, pages: list):
    """pages: 1-indexed list of pages to keep, in that order"""
    reader = PdfReader(storage.resolve(file_id))
    n = len(reader.pages)
    clean_pages = parse_pages(pages, n, allow_none=False)
    writer = PdfWriter()
    writer.append(reader, pages=[p - 1 for p in clean_pages])
    copy_metadata_and_forms(reader, writer)
    return _write(writer)


def page_count(file_id):
    return len(PdfReader(storage.resolve(file_id)).pages)


def _write(writer: PdfWriter):
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(path, ".pdf")
