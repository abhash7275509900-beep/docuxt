"""
Duplicate page detection.

Uses text similarity (with quick_ratio pre-filtering) when a page has
meaningful extractable text, and falls back to perceptual dHash on
image-only/scanned pages.
Accepts `threshold` either as a similarity ratio in (0, 1] (applied to
both text and image pages) or as an integer Hamming distance in [0, 256],
plus optional explicit `text_threshold` and `image_distance` parameters.
"""
import difflib
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

from app import storage
from app.operations import parse_finite_float

TEXT_SIMILARITY_THRESHOLD = 0.95
IMAGE_HASH_SIZE = 16
IMAGE_HASH_MAX_DISTANCE = 10  # out of IMAGE_HASH_SIZE**2 (256) bits
MIN_TEXT_LENGTH_TO_COMPARE_AS_TEXT = 20


def _dhash(pil_image, size=IMAGE_HASH_SIZE):
    small = pil_image.convert("L").resize((size + 1, size), Image.LANCZOS)
    pixels = list(small.getdata())
    bits = []
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits.append("1" if left > right else "0")
    return "".join(bits)


def _hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def _page_signature(text, pdfium_page):
    text = (text or "").strip()
    if len(text) >= MIN_TEXT_LENGTH_TO_COMPARE_AS_TEXT:
        return ("text", text)
    bitmap = pdfium_page.render(scale=1.0)
    return ("image", _dhash(bitmap.to_pil()))


def find_duplicate_pages(file_id, threshold=None, text_threshold=None, image_distance=None):
    text_sim_threshold = TEXT_SIMILARITY_THRESHOLD
    max_image_distance = IMAGE_HASH_MAX_DISTANCE
    total_bits = IMAGE_HASH_SIZE * IMAGE_HASH_SIZE

    if threshold is not None:
        val = parse_finite_float(threshold, "threshold", min_val=0.0, max_val=float(total_bits))
        if isinstance(threshold, float) and 0.0 < val <= 1.0:
            text_sim_threshold = val
            max_image_distance = round((1.0 - val) * total_bits)
        else:
            max_image_distance = val

    if text_threshold is not None:
        text_sim_threshold = parse_finite_float(
            text_threshold, "text_threshold", min_val=0.0, max_val=1.0, exclusive_min=True
        )
    if image_distance is not None:
        max_image_distance = parse_finite_float(
            image_distance, "image_distance", min_val=0.0, max_val=float(total_bits)
        )

    src = storage.resolve(file_id)
    pdfium_doc = pdfium.PdfDocument(src)

    signatures = []
    try:
        with pdfplumber.open(src) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                signatures.append(_page_signature(text, pdfium_doc[i]))
    finally:
        if hasattr(pdfium_doc, "close"):
            try:
                pdfium_doc.close()
            except Exception:
                pass

    def is_dup(sig_a, sig_b):
        kind_a, val_a = sig_a
        kind_b, val_b = sig_b
        if kind_a != kind_b:
            return False
        if kind_a == "text":
            matcher = difflib.SequenceMatcher(None, val_a, val_b)
            if matcher.quick_ratio() < text_sim_threshold:
                return False
            return matcher.ratio() >= text_sim_threshold
        return _hamming(val_a, val_b) <= max_image_distance

    groups = []
    seen = set()
    for i in range(len(signatures)):
        if i in seen:
            continue
        group = [i + 1]
        for j in range(i + 1, len(signatures)):
            if j in seen:
                continue
            if is_dup(signatures[i], signatures[j]):
                group.append(j + 1)
                seen.add(j)
        if len(group) > 1:
            groups.append(group)
            seen.add(i)

    return {"total_pages": len(signatures), "duplicate_groups": groups}
