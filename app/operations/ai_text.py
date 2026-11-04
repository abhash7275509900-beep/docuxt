"""
AI-assisted features — Phase 7.

Extractive summarization, TF-IDF cosine-similarity Q&A search, and
regex + Luhn-checksum PII detection (with full support for rotated and
cropped pages in displayed coordinate space).
"""
import re
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import storage
from app.operations import unrotated_to_displayed_rect

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9])')


def _extract_sentences_by_page(file_id):
    src = storage.resolve(file_id)
    sentences = []
    fallback_sentences = []
    with pdfplumber.open(src) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            text = text.replace("\n", " ")
            for raw in _SENTENCE_SPLIT_RE.split(text):
                s = raw.strip()
                if len(s) >= 15:
                    sentences.append({"page": page_num, "text": s})
                elif s:
                    fallback_sentences.append({"page": page_num, "text": s})
    return sentences if sentences else fallback_sentences


def _fit_tfidf(texts):
    try:
        vectorizer = TfidfVectorizer(stop_words="english")
        return vectorizer.fit_transform(texts)
    except ValueError:
        vectorizer = TfidfVectorizer(stop_words=None)
        return vectorizer.fit_transform(texts)


def summarize(file_id, num_sentences: int = 3):
    if isinstance(num_sentences, bool) or not isinstance(num_sentences, int) or num_sentences < 1:
        raise ValueError("sentences must be a positive integer")
    sentences = _extract_sentences_by_page(file_id)
    if len(sentences) <= num_sentences:
        summary_text = " ".join(s["text"] for s in sentences)
        return storage.save_output_bytes(summary_text.encode("utf-8"), ".txt")

    texts = [s["text"] for s in sentences]
    try:
        tfidf = _fit_tfidf(texts)
        centroid = tfidf.mean(axis=0)
        centroid = _as_2d_array(centroid)
        scores = cosine_similarity(tfidf, centroid).flatten()
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:num_sentences]
        top_indices.sort()
    except Exception:
        top_indices = list(range(num_sentences))

    summary_text = " ".join(sentences[i]["text"] for i in top_indices)
    return storage.save_output_bytes(summary_text.encode("utf-8"), ".txt")


def ask(file_id, question: str, top_k: int = 3):
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    sentences = _extract_sentences_by_page(file_id)
    if not sentences:
        return {"question": question, "matches": []}

    texts = [s["text"] for s in sentences]
    try:
        tfidf = _fit_tfidf(texts + [question])
        doc_vectors, question_vector = tfidf[:-1], tfidf[-1]
        scores = cosine_similarity(doc_vectors, question_vector).flatten()
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        matches = [
            {"page": sentences[i]["page"], "text": sentences[i]["text"],
             "score": round(float(scores[i]), 4)}
            for i in ranked if scores[i] > 0
        ]
    except Exception:
        matches = []
    return {"question": question, "matches": matches}


def _as_2d_array(sparse_mean):
    import numpy as np
    return np.asarray(sparse_mean)


# ---------- PII detection ----------

_PII_PATTERNS = {
    "ssn": re.compile(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)'),
    "email": re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'),
    "phone": re.compile(r'(?<!\w)(?:\(\d{3}\)[ \t]?|\d{3}[-. \t])\d{3}[-. \t]\d{4}(?!\d)'),
    "credit_card": re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)'),
}


def _luhn_valid(digit_string):
    digits = [int(d) for d in digit_string if d.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def detect_pii(file_id):
    """Returns [{page, type, text, x, y, width, height}, ...] in displayed
    bottom-left-origin PDF-point coordinates (accounting for /Rotate and /CropBox),
    matching the coordinate space expected by /pdf/redact."""
    src = storage.resolve(file_id)
    findings = []

    reader = PdfReader(src)
    pdfium_doc = pdfium.PdfDocument(src)
    try:
        for page_idx, pypdf_page in enumerate(reader.pages):
            page_num = page_idx + 1
            rot = int(pypdf_page.get("/Rotate", 0) or 0) % 360
            crop = pypdf_page.cropbox
            c_left = float(crop.left)
            c_bottom = float(crop.bottom)
            W = float(crop.width)
            H = float(crop.height)

            tp = pdfium_doc[page_idx].get_textpage()
            page_text = tp.get_text_range() or ""
            if not page_text.strip():
                continue

            for pii_type, pattern in _PII_PATTERNS.items():
                for match in pattern.finditer(page_text):
                    matched_str = match.group()
                    if pii_type == "credit_card" and not _luhn_valid(matched_str):
                        continue
                    start_idx = match.start()
                    char_count = match.end() - start_idx
                    n_rects = tp.count_rects(start_idx, char_count)
                    rects = [tp.get_rect(i) for i in range(n_rects)]
                    if not rects:
                        rects = [tp.get_charbox(i) for i in range(start_idx, match.end())]
                    if not rects:
                        continue

                    ux0 = min(r[0] for r in rects) - c_left
                    uy0 = min(r[1] for r in rects) - c_bottom
                    ux1 = max(r[2] for r in rects) - c_left
                    uy1 = max(r[3] for r in rects) - c_bottom

                    dx0, dy0, dx1, dy1 = unrotated_to_displayed_rect(
                        rot, ux0, uy0, ux1, uy1, W, H
                    )
                    findings.append({
                        "page": page_num,
                        "type": pii_type,
                        "text": matched_str,
                        "x": round(dx0, 2),
                        "y": round(dy0, 2),
                        "width": round(max(1.0, dx1 - dx0), 2),
                        "height": round(max(1.0, dy1 - dy0), 2),
                    })
    finally:
        if hasattr(pdfium_doc, "close"):
            try:
                pdfium_doc.close()
            except Exception:
                pass

    return findings
