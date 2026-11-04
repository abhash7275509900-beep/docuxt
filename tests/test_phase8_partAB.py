import os
"""
Regression test — Phase 8 Part A (metadata) + Part B (bookmarks & hyperlinks).
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


def make_blank_pdf_bytes(n_pages=1):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_text_pdf_bytes(text):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.drawString(72, 700, text)
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase8ab_test@test.com", "password": "testpass12"})
token = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase8ab_test@test.com", "password": "testpass12"}).json()["token"]
check("test user authenticated", bool(token))


# ============================================================
print("=" * 60)
print("Part A — Document metadata")
print("=" * 60)

fid = upload(token, "meta.pdf", make_text_pdf_bytes("Metadata test")).json()["file_id"]

before = requests.get(f"{BASE}/pdf/{fid}/metadata", headers=auth_header(token)).json()
check("metadata read succeeds before any edits", "title" in before, str(before))

r = requests.post(f"{BASE}/pdf/metadata", headers=auth_header(token),
                   json={"file_id": fid,
                         "fields": {"title": "Q3 Report", "author": "Jane Analyst",
                                    "keywords": "finance, quarterly"},
                         "custom": {"Department": "Finance"}})
result = wait_job(token, r.json()["job_id"])
check("set metadata job succeeds", result["status"] == "done", str(result))
new_fid = result.get("result")

after = requests.get(f"{BASE}/pdf/{new_fid}/metadata", headers=auth_header(token)).json()
check("title set correctly", after.get("title") == "Q3 Report", str(after))
check("author set correctly", after.get("author") == "Jane Analyst", str(after))
check("keywords set correctly", after.get("keywords") == "finance, quarterly", str(after))
check("custom field set correctly", after.get("custom", {}).get("Department") == "Finance", str(after))

# removing a field with null
r2 = requests.post(f"{BASE}/pdf/metadata", headers=auth_header(token),
                    json={"file_id": new_fid, "fields": {"author": None}})
result2 = wait_job(token, r2.json()["job_id"])
after2 = requests.get(f"{BASE}/pdf/{result2['result']}/metadata", headers=auth_header(token)).json()
check("setting a field to null removes it", not after2.get("author"), str(after2))
check("other fields survive a partial update untouched",
      after2.get("title") == "Q3 Report", str(after2))

bad_field = requests.post(f"{BASE}/pdf/metadata", headers=auth_header(token),
                           json={"file_id": fid, "fields": {"not_a_real_field": "x"}})
bad_result = wait_job(token, bad_field.json()["job_id"])
check("unknown standard field name rejected", bad_result["status"] == "failed", str(bad_result))


# ============================================================
print("=" * 60)
print("Part B — Bookmarks (with nesting)")
print("=" * 60)

fid_bm = upload(token, "bookmarks.pdf", make_blank_pdf_bytes(5)).json()["file_id"]

r = requests.post(f"{BASE}/pdf/bookmarks", headers=auth_header(token),
                   json={"file_id": fid_bm, "title": "Introduction", "page": 1})
result = wait_job(token, r.json()["job_id"])
check("add top-level bookmark succeeds", result["status"] == "done", str(result))
fid_bm2 = result.get("result")

r2 = requests.post(f"{BASE}/pdf/bookmarks", headers=auth_header(token),
                    json={"file_id": fid_bm2, "title": "Chapter 1", "page": 3})
result2 = wait_job(token, r2.json()["job_id"])
fid_bm3 = result2.get("result")

r3 = requests.post(f"{BASE}/pdf/bookmarks", headers=auth_header(token),
                    json={"file_id": fid_bm3, "title": "Section 1.1", "page": 4,
                          "parent_title": "Chapter 1"})
result3 = wait_job(token, r3.json()["job_id"])
check("add nested bookmark succeeds", result3["status"] == "done", str(result3))
fid_bm4 = result3.get("result")

listing = requests.get(f"{BASE}/pdf/{fid_bm4}/bookmarks", headers=auth_header(token)).json()
bookmarks = listing.get("bookmarks", [])
check("all 3 bookmarks present (this caught a real nesting bug during development)",
      len(bookmarks) == 3, str(bookmarks))
titles_depths = {b["title"]: b["depth"] for b in bookmarks}
check("Introduction and Chapter 1 are top-level (depth 0)",
      titles_depths.get("Introduction") == 0 and titles_depths.get("Chapter 1") == 0,
      str(titles_depths))
check("Section 1.1 is correctly nested (depth 1)",
      titles_depths.get("Section 1.1") == 1, str(titles_depths))

bad_parent = requests.post(f"{BASE}/pdf/bookmarks", headers=auth_header(token),
                            json={"file_id": fid_bm4, "title": "Orphan", "page": 2,
                                  "parent_title": "Does Not Exist"})
bad_parent_result = wait_job(token, bad_parent.json()["job_id"])
check("nesting under a nonexistent parent fails cleanly", bad_parent_result["status"] == "failed",
      str(bad_parent_result))


# ============================================================
print("=" * 60)
print("Part B — Hyperlinks")
print("=" * 60)

fid_links = upload(token, "links.pdf", make_blank_pdf_bytes(3)).json()["file_id"]

r = requests.post(f"{BASE}/pdf/hyperlinks", headers=auth_header(token),
                   json={"file_id": fid_links, "page": 1, "x": 72, "y": 700,
                         "width": 150, "height": 20, "url": "https://example.com/docs"})
result = wait_job(token, r.json()["job_id"])
check("add external URL hyperlink succeeds", result["status"] == "done", str(result))
fid_links2 = result.get("result")

r2 = requests.post(f"{BASE}/pdf/hyperlinks", headers=auth_header(token),
                    json={"file_id": fid_links2, "page": 1, "x": 72, "y": 650,
                          "width": 100, "height": 20, "target_page": 3})
result2 = wait_job(token, r2.json()["job_id"])
check("add internal page-link succeeds", result2["status"] == "done", str(result2))
fid_links3 = result2.get("result")

listing = requests.get(f"{BASE}/pdf/{fid_links3}/hyperlinks", headers=auth_header(token)).json()
links = listing.get("hyperlinks", [])
check("both hyperlinks present", len(links) == 2, str(links))
url_link = next((l for l in links if "url" in l), None)
internal_link = next((l for l in links if l.get("internal_dest")), None)
check("URL link has the correct URL", url_link and url_link["url"] == "https://example.com/docs",
      str(url_link))
check("internal link is flagged as internal_dest", internal_link is not None, str(links))
check("URL link rect coordinates are accurate",
      url_link and url_link["rect"] == [72.0, 700.0, 222.0, 720.0], str(url_link))

both_given = requests.post(f"{BASE}/pdf/hyperlinks", headers=auth_header(token),
                            json={"file_id": fid_links3, "page": 1, "x": 0, "y": 0,
                                  "width": 10, "height": 10,
                                  "url": "https://x.com", "target_page": 2})
both_result = wait_job(token, both_given.json()["job_id"])
check("providing both url and target_page fails cleanly", both_result["status"] == "failed",
      str(both_result))

neither_given = requests.post(f"{BASE}/pdf/hyperlinks", headers=auth_header(token),
                               json={"file_id": fid_links3, "page": 1, "x": 0, "y": 0,
                                     "width": 10, "height": 10})
neither_result = wait_job(token, neither_given.json()["job_id"])
check("providing neither url nor target_page fails cleanly", neither_result["status"] == "failed",
      str(neither_result))


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
