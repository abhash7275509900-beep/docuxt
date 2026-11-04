"""
Document comparison and analysis.

compare(): text-level diff between two PDFs (difflib) plus basic stats.
analyze(): standalone stats for one document — page count, word count,
and Flesch Reading Ease (returns None / "n/a" when word_count == 0).
"""
import re
import difflib
import pdfplumber

from app import storage


def _full_text(file_id):
    src = storage.resolve(file_id)
    pages = []
    with pdfplumber.open(src) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def compare(file_id_a, file_id_b):
    pages_a = _full_text(file_id_a)
    pages_b = _full_text(file_id_b)
    text_a = "\n".join(pages_a)
    text_b = "\n".join(pages_b)

    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()
    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    similarity = round(matcher.ratio(), 4)

    diff_lines = list(difflib.unified_diff(
        lines_a, lines_b, fromfile="version_a", tofile="version_b", lineterm=""
    ))
    # Skip only the first two unified_diff header lines ("--- version_a", "+++ version_b")
    # so content lines starting with "--" (which become "---...") or "++" are counted properly.
    body_diff = diff_lines[2:]
    added = sum(1 for l in body_diff if l.startswith("+"))
    removed = sum(1 for l in body_diff if l.startswith("-"))

    return {
        "similarity": similarity,
        "pages_a": len(pages_a),
        "pages_b": len(pages_b),
        "words_a": len(text_a.split()),
        "words_b": len(text_b.split()),
        "lines_added": added,
        "lines_removed": removed,
        "diff": diff_lines[:500],
    }


_VOWEL_RE = re.compile(r'[aeiouyAEIOUY]+')


def _count_syllables(word):
    word = word.lower().strip(".,!?;:\"'()")
    if not word:
        return 0
    matches = _VOWEL_RE.findall(word)
    count = len(matches)
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def analyze(file_id):
    pages = _full_text(file_id)
    full_text = "\n".join(pages)
    words = full_text.split()
    sentences = re.split(r'[.!?]+', full_text)
    sentences = [s for s in sentences if s.strip()]

    word_count = len(words)
    if word_count == 0:
        return {
            "page_count": len(pages),
            "word_count": 0,
            "sentence_count": 0,
            "flesch_reading_ease": None,
            "reading_level": "n/a",
        }

    sentence_count = max(len(sentences), 1)
    syllable_count = sum(_count_syllables(w) for w in words) or 1

    words_per_sentence = word_count / sentence_count
    syllables_per_word = syllable_count / word_count
    flesch_score = 206.835 - (1.015 * words_per_sentence) - (84.6 * syllables_per_word)

    return {
        "page_count": len(pages),
        "word_count": word_count,
        "sentence_count": sentence_count,
        "flesch_reading_ease": round(flesch_score, 1),
        "reading_level": _flesch_label(flesch_score),
    }


def _flesch_label(score):
    if score >= 90:
        return "very easy (5th grade)"
    if score >= 70:
        return "easy (7th grade)"
    if score >= 60:
        return "standard (8th-9th grade)"
    if score >= 30:
        return "difficult (college)"
    return "very difficult (college graduate)"
