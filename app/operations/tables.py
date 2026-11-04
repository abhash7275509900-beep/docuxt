"""
Table extraction — detects tables via pdfplumber and exports them to a
real .xlsx workbook (one sheet per table) or .csv file.
Sanitizes control characters and prevents spreadsheet formula injection
while preserving genuine signed numbers (-12.5, +3) in CSV exports.
"""
import csv
import io
import os
import re
import tempfile
import pdfplumber
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from app import storage
from app.operations import parse_pages

ALLOWED_FORMATS = {"xlsx", "csv"}
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?$")


def _clean_cell_text(val) -> str:
    if val is None:
        return ""
    s = str(val)
    return ILLEGAL_CHARACTERS_RE.sub("", s)


def _csv_safe(val) -> str:
    s = _clean_cell_text(val)
    stripped = s.strip()
    if stripped and _PLAIN_NUMBER_RE.match(stripped):
        return s
    if s and s[0] in _FORMULA_PREFIXES:
        return "'" + s
    return s


def extract_tables(file_id, fmt: str = "xlsx", pages: list = None):
    """pages: optional list of 1-indexed page numbers (default: all).
    Returns {file_id, tables_found}."""
    if not isinstance(fmt, str):
        raise ValueError("format must be a string")
    fmt = fmt.lower()
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(f"format must be one of {sorted(ALLOWED_FORMATS)}")

    src = storage.resolve(file_id)
    tables = []
    with pdfplumber.open(src) as pdf:
        n_pages = len(pdf.pages)
        validated_pages = parse_pages(pages, total_pages=n_pages, allow_none=True, allow_empty=False)
        target_pages = set(validated_pages) if validated_pages is not None else None

        for page_num, page in enumerate(pdf.pages, start=1):
            if target_pages is not None and page_num not in target_pages:
                continue
            for tbl in page.extract_tables():
                cleaned = [
                    [_clean_cell_text(cell) for cell in row]
                    for row in tbl
                ]
                if cleaned:
                    tables.append((page_num, cleaned))

    if fmt == "xlsx":
        wb = Workbook()
        default_sheet = wb.active
        if tables:
            for idx, (page_num, rows) in enumerate(tables, start=1):
                ws = default_sheet if idx == 1 else wb.create_sheet()
                ws.title = f"Table{idx}_p{page_num}"
                for r_idx, row in enumerate(rows, start=1):
                    for c_idx, val in enumerate(row, start=1):
                        s = _clean_cell_text(val)
                        cell = ws.cell(row=r_idx, column=c_idx)
                        cell.value = s
                        cell.data_type = "s"
        else:
            default_sheet.title = "NoTablesFound"

        fd, out_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        wb.save(out_path)
        out_id = storage.save_output_path(out_path, ".xlsx")
    else:  # csv
        buf = io.StringIO()
        writer = csv.writer(buf)
        for idx, (page_num, rows) in enumerate(tables, start=1):
            if idx > 1:
                writer.writerow([])
                writer.writerow([f"# Table {idx} (page {page_num})"])
            for row in rows:
                writer.writerow([_csv_safe(v) for v in row])
        out_id = storage.save_output_bytes(buf.getvalue().encode("utf-8"), ".csv")

    return {"file_id": out_id, "tables_found": len(tables)}
