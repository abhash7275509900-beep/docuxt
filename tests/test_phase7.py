import os
"""
Regression test — Phase 7 (AI-assisted, offline/free-only).
"""
import sys
import io
import requests
import pdfplumber

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


def make_pdf_bytes(lines, font_size=12, size=(612, 792)):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.setFont("Helvetica", font_size)
    y = size[1] - 92
    for line in lines:
        c.drawString(72, y, line)
        y -= 30
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase7test@test.com", "password": "testpass12"})
token = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase7test@test.com", "password": "testpass12"}).json()["token"]
check("test user authenticated", bool(token))


# ============================================================
print("=" * 60)
print("Summarization (extractive, TF-IDF centroid)")
print("=" * 60)

article_lines = [
    "The city council voted on Tuesday to approve funding for a new public library.",
    "The library will be built downtown near the central train station.",
    "Construction is expected to begin in early spring and take about eighteen months.",
    "Local residents have expressed strong support for the project in community meetings.",
    "The mayor said the library will include a childrens reading room and a maker space.",
    "Critics have raised concerns about the projected cost exceeding twelve million dollars.",
    "A final budget review is scheduled for next month before construction contracts are signed.",
    "The current library building, built in 1962, will be converted into a community center.",
]
fid_article = upload(token, "article.pdf", make_pdf_bytes(article_lines)).json()["file_id"]

r = requests.post(f"{BASE}/ai/summarize", headers=auth_header(token),
                   json={"file_id": fid_article, "sentences": 3})
result = wait_job(token, r.json()["job_id"])
check("summarize job succeeds", result["status"] == "done", str(result))
summary_id = result.get("result")
if summary_id:
    summary_text = requests.get(f"{BASE}/files/{summary_id}", headers=auth_header(token)).content.decode()
    sentence_count = summary_text.count(". ") + (1 if summary_text.strip() else 0)
    check("summary contains real sentences from the source document",
          any(line.rstrip(".") in summary_text for line in article_lines), repr(summary_text))
    check("summary is shorter than the full document",
          len(summary_text) < len(" ".join(article_lines)), f"summary len={len(summary_text)}")

# asking for more sentences than exist shouldn't crash
short_fid = upload(token, "short.pdf", make_pdf_bytes(["Just one short sentence here."])).json()["file_id"]
r = requests.post(f"{BASE}/ai/summarize", headers=auth_header(token),
                   json={"file_id": short_fid, "sentences": 10})
result = wait_job(token, r.json()["job_id"])
check("summarize on a doc shorter than requested length doesn't crash",
      result["status"] == "done", str(result))


# ============================================================
print("=" * 60)
print("Document Q&A (extractive TF-IDF search, not generative)")
print("=" * 60)

r = requests.post(f"{BASE}/ai/ask", headers=auth_header(token),
                   json={"file_id": fid_article, "question": "how much will the library cost", "top_k": 3})
result = wait_job(token, r.json()["job_id"])
check("ask job succeeds", result["status"] == "done", str(result))
matches = (result.get("result") or {}).get("matches", [])
check("ask returns ranked matches", len(matches) > 0, str(matches))
check("top match for a cost question is the cost sentence",
      matches and "cost" in matches[0]["text"].lower() and "twelve million" in matches[0]["text"],
      str(matches[0] if matches else None))
check("matches are score-sorted descending",
      all(matches[i]["score"] >= matches[i+1]["score"] for i in range(len(matches)-1)))

r2 = requests.post(f"{BASE}/ai/ask", headers=auth_header(token),
                    json={"file_id": fid_article, "question": "when does construction start", "top_k": 2})
result2 = wait_job(token, r2.json()["job_id"])
matches2 = (result2.get("result") or {}).get("matches", [])
check("top match for a timing question is the construction-start sentence",
      matches2 and "spring" in matches2[0]["text"].lower(), str(matches2))

# a question with no real match in the document should still respond cleanly
r3 = requests.post(f"{BASE}/ai/ask", headers=auth_header(token),
                    json={"file_id": fid_article, "question": "xyz", "top_k": 3})
result3 = wait_job(token, r3.json()["job_id"])
check("ask with a near-nonsense query doesn't crash", result3["status"] == "done", str(result3))


# ============================================================
print("=" * 60)
print("PII detection (regex + Luhn checksum, feeds POST /pdf/redact)")
print("=" * 60)

pii_lines = [
    "Customer Intake Form",
    "Name: John Smith",
    "Email: john.smith@example.com",
    "Phone: 555-867-5309",
    "SSN: 123-45-6789",
    "Card number: 4532015112830366",  # real Luhn-valid test card number
    "Notes: customer requested a callback next week.",
]
fid_pii = upload(token, "pii.pdf", make_pdf_bytes(pii_lines)).json()["file_id"]

r = requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(token), json={"file_id": fid_pii})
result = wait_job(token, r.json()["job_id"])
check("detect-pii job succeeds", result["status"] == "done", str(result))
findings = result.get("result") or []
check("detects exactly 4 PII items (email, phone, ssn, credit_card)", len(findings) == 4, str(findings))

found_types = {f["type"] for f in findings}
check("all four expected PII types detected",
      found_types == {"email", "phone", "ssn", "credit_card"}, str(found_types))

email_finding = next((f for f in findings if f["type"] == "email"), None)
check("email finding has correct matched text",
      email_finding and email_finding["text"] == "john.smith@example.com", str(email_finding))

# false positive resistance: invalid Luhn card number should NOT be flagged
edge_lines = [
    "Invoice number: 4532015112830367",  # 16 digits, INVALID Luhn checksum
    "Order date: 12-25-2024",
    "Reference: ABC-123-XYZ",
    "Contact us at support@ourcompany.org for help",
]
fid_edge = upload(token, "edge.pdf", make_pdf_bytes(edge_lines)).json()["file_id"]
r = requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(token), json={"file_id": fid_edge})
result = wait_job(token, r.json()["job_id"])
edge_findings = result.get("result") or []
check("Luhn-invalid 16-digit number is NOT flagged as a credit card (checksum working)",
      not any(f["type"] == "credit_card" for f in edge_findings), str(edge_findings))
check("only the genuine email is flagged among the edge cases",
      len(edge_findings) == 1 and edge_findings[0]["type"] == "email", str(edge_findings))

# the real end-to-end proof: feed PII findings straight into /pdf/redact with zero conversion
regions = [{"page": f["page"], "x": f["x"], "y": f["y"], "width": f["width"], "height": f["height"]}
           for f in findings]
r = requests.post(f"{BASE}/pdf/redact", headers=auth_header(token),
                   json={"file_id": fid_pii, "regions": regions})
result = wait_job(token, r.json()["job_id"], timeout=30)
check("redact using raw PII-detection output (no coordinate conversion) succeeds",
      result["status"] == "done", str(result))
redacted_id = (result.get("result") or {}).get("file_id")
if redacted_id:
    content = requests.get(f"{BASE}/files/{redacted_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    check("redacted page has zero extractable text (whole-page rasterization, as designed)",
          text.strip() == "", repr(text))

fake_pdf_check = requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(token),
                                json={"file_id": "doesnotexist.pdf"})
fake_result = wait_job(token, fake_pdf_check.json()["job_id"]) if fake_pdf_check.status_code == 200 else None
check("detect-pii on nonexistent file rejected before job (403/404) or fails cleanly",
      fake_pdf_check.status_code in (403, 404) or (fake_result and fake_result["status"] == "failed"),
      f"status={fake_pdf_check.status_code}")


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
