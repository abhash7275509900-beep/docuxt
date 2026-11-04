"""
Lightweight extraction — Phase 3, Part B.

These don't touch LibreOffice at all: pdfplumber pulls the text layer
directly, which is near-instant and good enough for "get the text
out" use cases (search indexing, quick previews, feeding an LLM)
where DOCX-level layout fidelity from office_convert.py is overkill.

pdf_to_text  -> plain .txt, pages joined with a form-feed-ish marker
pdf_to_html  -> minimal per-page <div>, paragraphs preserved
pdf_to_markdown -> same structure, markdown headings per page
"""
import html as html_lib
import pdfplumber
from app import storage


def pdf_to_text(file_id):
    src = storage.resolve(file_id)
    parts = []
    with pdfplumber.open(src) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            parts.append(f"--- page {i} ---\n{text}")
    out = "\n\n".join(parts)
    return storage.save_output_bytes(out.encode("utf-8"), ".txt")


def pdf_to_html(file_id):
    src = storage.resolve(file_id)
    chunks = ["<html><body>"]
    with pdfplumber.open(src) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            escaped = html_lib.escape(text)
            paragraphs = "".join(
                f"<p>{line}</p>" for line in escaped.split("\n") if line.strip()
            )
            chunks.append(f'<div class="page" data-page="{i}">{paragraphs}</div>')
    chunks.append("</body></html>")
    out = "\n".join(chunks)
    return storage.save_output_bytes(out.encode("utf-8"), ".html")


def pdf_to_markdown(file_id):
    src = storage.resolve(file_id)
    parts = []
    with pdfplumber.open(src) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            parts.append(f"## Page {i}\n\n{text}")
    out = "\n\n".join(parts)
    return storage.save_output_bytes(out.encode("utf-8"), ".md")
