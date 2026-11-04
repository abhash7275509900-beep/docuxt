import os
"""
Regression test — Phase 8 Part D (compare, analyze, duplicate pages).
"""
import sys
import io
import requests

BASE = os.environ.get("DOCENGINE_BASE_URL", "http://localhost:8000")
results = []


def check(desc, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, desc, detail))
    print(f"[{status}] {desc}" + (f"  -- {detail}" if detail and status == 'FAIL' else ""))
    return condition


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def wait_job(token, job_id, timeout=30):
    import time
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}", headers=auth_header(token))
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.2)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    return requests.post(f"{BASE}/files", headers=auth_header(token),
                          files={"file": (filename, io.BytesIO(content_bytes))})


def make_pdf_bytes(lines, font_size=12):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", font_size)
    y = 700
    for line in lines:
        c.drawString(72, y, line)
        y -= 24
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase8d_test@test.com", "password": "testpass12"})
token = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase8d_test@test.com", "password": "testpass12"}).json()["token"]
check("test user authenticated", bool(token))


# ============================================================
print("=" * 60)
print("Document comparison")
print("=" * 60)

v1_lines = [
    "Section 1: Introduction",
    "This document describes the new product launch plan.",
    "The launch date is set for March 2025.",
    "Budget approval is pending from finance.",
    "Section 2: Timeline",
    "Design phase completes in January.",
]
v2_lines = [
    "Section 1: Introduction",
    "This document describes the new product launch plan.",
    "The launch date is set for April 2025.",
    "Budget approval has been granted by finance.",
    "Section 2: Timeline",
    "Design phase completes in January.",
    "Section 3: Risks",
    "Supply chain delays are the primary risk factor.",
]
fid_a = upload(token, "v1.pdf", make_pdf_bytes(v1_lines)).json()["file_id"]
fid_b = upload(token, "v2.pdf", make_pdf_bytes(v2_lines)).json()["file_id"]

r = requests.post(f"{BASE}/pdf/compare", headers=auth_header(token),
                   json={"file_id_a": fid_a, "file_id_b": fid_b})
result = wait_job(token, r.json()["job_id"])
check("compare job succeeds", result["status"] == "done", str(result))
cmp = result.get("result") or {}
check("word counts exactly match hand-count (33 vs 45)",
      cmp.get("words_a") == 33 and cmp.get("words_b") == 45, str(cmp))
check("lines_added exactly matches expected (4: 2 changed + 2 new)",
      cmp.get("lines_added") == 4, str(cmp))
check("lines_removed exactly matches expected (2: the 2 changed lines)",
      cmp.get("lines_removed") == 2, str(cmp))
check("similarity ratio exactly matches formula 2*4/14",
      cmp.get("similarity") == round(2 * 4 / 14, 4), str(cmp))
check("diff contains the actual removed line", any("March 2025" in l for l in cmp.get("diff", [])))
check("diff contains the actual added line", any("April 2025" in l for l in cmp.get("diff", [])))

identical = upload(token, "same.pdf", make_pdf_bytes(v1_lines)).json()["file_id"]
r2 = requests.post(f"{BASE}/pdf/compare", headers=auth_header(token),
                    json={"file_id_a": fid_a, "file_id_b": identical})
result2 = wait_job(token, r2.json()["job_id"])
check("comparing a document to an identical copy gives similarity 1.0",
      (result2.get("result") or {}).get("similarity") == 1.0, str(result2))


# ============================================================
print("=" * 60)
print("Document analysis (readability)")
print("=" * 60)

simple_lines = ["The cat sat on the mat.", "The dog ran in the sun.",
                 "I like to eat cake.", "She has a red bike."]
complex_lines = [
    "The multifaceted organizational restructuring necessitates comprehensive stakeholder",
    "engagement methodologies incorporating interdisciplinary collaborative frameworks.",
    "Subsequently, the aforementioned implementation strategies require substantiated",
    "documentation demonstrating quantifiable improvements across heterogeneous departments.",
]
fid_simple = upload(token, "simple.pdf", make_pdf_bytes(simple_lines)).json()["file_id"]
fid_complex = upload(token, "complex.pdf", make_pdf_bytes(complex_lines)).json()["file_id"]

simple_stats = requests.get(f"{BASE}/pdf/{fid_simple}/analyze", headers=auth_header(token)).json()
complex_stats = requests.get(f"{BASE}/pdf/{fid_complex}/analyze", headers=auth_header(token)).json()

check("simple text word count exactly matches hand-count (22)",
      simple_stats.get("word_count") == 22, str(simple_stats))
check("complex text word count exactly matches hand-count (27)",
      complex_stats.get("word_count") == 27, str(complex_stats))
check("simple text scores meaningfully higher (easier) than complex text",
      simple_stats["flesch_reading_ease"] > complex_stats["flesch_reading_ease"] + 50,
      f"simple={simple_stats['flesch_reading_ease']} complex={complex_stats['flesch_reading_ease']}")
check("simple text labeled easy, complex text labeled difficult",
      "easy" in simple_stats["reading_level"] and "difficult" in complex_stats["reading_level"])


# ============================================================
print("=" * 60)
print("Duplicate page detection")
print("=" * 60)

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


def _text_page(text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 16)
    c.drawString(100, 400, text)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


w = PdfWriter()
p_alpha = _text_page("This is document page ALPHA with some real sentence content.")
p_beta = _text_page("This is document page BETA describing something else entirely.")
p_gamma = _text_page("This is document page GAMMA covering a completely different topic.")
for p in (p_alpha, p_beta, p_alpha, p_gamma, p_beta):  # 1,2,3,4,5 -> dups at (1,3) and (2,5)
    w.add_page(p)
buf = io.BytesIO()
w.write(buf)
fid_dup = upload(token, "dup.pdf", buf.getvalue()).json()["file_id"]

r = requests.post(f"{BASE}/pdf/find-duplicate-pages", headers=auth_header(token),
                   json={"file_id": fid_dup})
result = wait_job(token, r.json()["job_id"])
check("duplicate detection job succeeds", result["status"] == "done", str(result))
groups = (result.get("result") or {}).get("duplicate_groups", [])
check("finds exactly the two known duplicate groups [1,3] and [2,5]",
      sorted(groups) == [[1, 3], [2, 5]], str(groups))

# a document with no duplicates at all should report an empty group list
w2 = PdfWriter()
for text in ("Unique page one content here.", "Unique page two content here.",
             "Unique page three content here."):
    w2.add_page(_text_page(text))
buf2 = io.BytesIO()
w2.write(buf2)
fid_nodup = upload(token, "nodup.pdf", buf2.getvalue()).json()["file_id"]
r2 = requests.post(f"{BASE}/pdf/find-duplicate-pages", headers=auth_header(token),
                    json={"file_id": fid_nodup})
result2 = wait_job(token, r2.json()["job_id"])
check("document with no duplicates reports an empty group list",
      (result2.get("result") or {}).get("duplicate_groups") == [], str(result2))


# ============================================================
print("=" * 60)
print("SUMMARY")
print("=" * 60)
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = [(d, det) for s, d, det in results if s == "FAIL"]
print(f"{passed}/{len(results)} checks passed")
if failed:
    print("FAILED CHECKS:")
    for d, det in failed:
        print(f"  - {d}" + (f"  [{det}]" if det else ""))
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
