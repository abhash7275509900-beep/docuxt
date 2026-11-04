import os
"""
Regression test — Bug Fixes & Frontend Integration.
Verifies all fixed backend bugs and frontend/preview serving endpoints.
"""
import io
import sys
import time
import requests
from PIL import Image

BASE = os.environ.get("DOCENGINE_BASE_URL", "http://localhost:8000")
results = []


def check(desc, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, desc, detail))
    print(f"[{status}] {desc}" + (f"  -- {detail}" if detail and status == "FAIL" else ""))
    return condition


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def wait_job(token, job_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}", headers=auth_header(token))
        if r.status_code != 200:
            return {"status": "http_error", "code": r.status_code}
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.2)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    return requests.post(
        f"{BASE}/files",
        headers=auth_header(token),
        files={"file": (filename, io.BytesIO(content_bytes))},
    )


def make_pdf_bytes(lines, pages=1):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for p in range(pages):
        c.setFont("Helvetica", 12)
        y = 700
        for line in lines:
            c.drawString(72, y, f"[P{p+1}] {line}")
            y -= 24
        c.showPage()
    c.save()
    return buf.getvalue()


# ============================================================
print("=" * 60)
print("Frontend & Static Asset Serving")
print("=" * 60)

r_idx = requests.get(f"{BASE}/")
check("GET / serves frontend HTML", r_idx.status_code == 200 and "DocEngine" in r_idx.text)

r_css = requests.get(f"{BASE}/frontend/styles.css")
check("GET /frontend/styles.css serves stylesheet", r_css.status_code == 200 and "--primary" in r_css.text)

r_js = requests.get(f"{BASE}/frontend/app.js")
check("GET /frontend/app.js serves frontend application script", r_js.status_code == 200 and "runJob" in r_js.text)


# ============================================================
print("=" * 60)
print("Bug Fixes — Auth, Job Isolation, Comment Resolve, Shared Docs")
print("=" * 60)

s1 = requests.post(f"{BASE}/auth/signup", json={"email": "bugfix_u1@test.com", "password": "password123"})
t1 = s1.json()["token"] if s1.status_code == 200 else requests.post(f"{BASE}/auth/login", json={"email": "bugfix_u1@test.com", "password": "password123"}).json()["token"]

s2 = requests.post(f"{BASE}/auth/signup", json={"email": "bugfix_u2@test.com", "password": "password123"})
t2 = s2.json()["token"] if s2.status_code == 200 else requests.post(f"{BASE}/auth/login", json={"email": "bugfix_u2@test.com", "password": "password123"}).json()["token"]

s3 = requests.post(f"{BASE}/auth/signup", json={"email": "bugfix_u3@test.com", "password": "password123"})
t3 = s3.json()["token"] if s3.status_code == 200 else requests.post(f"{BASE}/auth/login", json={"email": "bugfix_u3@test.com", "password": "password123"}).json()["token"]

fid_u1 = upload(t1, "u1_doc.pdf", make_pdf_bytes(["Hello from user 1"], pages=3)).json()["file_id"]

# Preview endpoint
prev = requests.get(f"{BASE}/files/{fid_u1}/preview?page=1&dpi=100", headers=auth_header(t1))
check("PDF page preview endpoint returns valid PNG", prev.status_code == 200 and prev.content[:8] == b"\x89PNG\r\n\x1a\n")

# Job ownership isolation: u2 cannot poll u1's job
job_r = requests.post(f"{BASE}/pdf/rotate", headers=auth_header(t1), json={"file_id": fid_u1, "degrees": 90})
jid = job_r.json()["job_id"]
u2_job_poll = requests.get(f"{BASE}/jobs/{jid}", headers=auth_header(t2))
check("other user cannot view/poll someone else's job status (403)", u2_job_poll.status_code == 403, u2_job_poll.text)
res_rot = wait_job(t1, jid)
rot_fid = res_rot.get("result")

# Derived file metadata has format="pdf" populated automatically
rot_info = requests.get(f"{BASE}/files/{rot_fid}/info", headers=auth_header(t1)).json()
check("derived file automatically gets format=pdf in metadata", rot_info.get("meta", {}).get("format") == "pdf", str(rot_info))

# Comment resolve authorization check
cm_r = requests.post(f"{BASE}/comments", headers=auth_header(t1), json={"file_id": fid_u1, "text": "Review item"})
cid = cm_r.json()["comment_id"]
unauth_resolve = requests.post(f"{BASE}/comments/{cid}/resolve", headers=auth_header(t3))
check("unrelated user cannot resolve someone else's comment (403)", unauth_resolve.status_code == 403, unauth_resolve.text)

# Shared document collaborators can download/preview document version files
doc_r = requests.post(f"{BASE}/documents", headers=auth_header(t1), json={"file_id": fid_u1, "title": "Shared Spec"})
doc_id = doc_r.json()["document_id"]
requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(t1), json={"email": "bugfix_u2@test.com", "permission": "edit"})

u2_dl_v1 = requests.get(f"{BASE}/files/{fid_u1}", headers=auth_header(t2))
check("collaborator can download document's v1 file (200)", u2_dl_v1.status_code == 200)

fid_u2_v2 = upload(t2, "u2_v2.pdf", make_pdf_bytes(["Collaborator v2 update"])).json()["file_id"]
requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(t2), json={"file_id": fid_u2_v2, "label": "u2 edit"})

u1_dl_v2 = requests.get(f"{BASE}/files/{fid_u2_v2}", headers=auth_header(t1))
check("document owner can download version file uploaded by collaborator (200)", u1_dl_v2.status_code == 200)

u3_dl_v2 = requests.get(f"{BASE}/files/{fid_u2_v2}", headers=auth_header(t3))
check("non-collaborator still blocked from downloading shared doc files (403)", u3_dl_v2.status_code == 403)


# ============================================================
print("=" * 60)
print("Bug Fixes — Input Validation, Page Bounds, RGBA & Grayscale")
print("=" * 60)

empty_merge = requests.post(f"{BASE}/pdf/merge", headers=auth_header(t1), json={"file_ids": []})
check("empty file_ids in /pdf/merge returns 400 (not 500 IndexError)", empty_merge.status_code == 400)

empty_i2p = requests.post(f"{BASE}/convert/images-to-pdf", headers=auth_header(t1), json={"file_ids": []})
check("empty file_ids in /convert/images-to-pdf returns 400 (not 500 IndexError)", empty_i2p.status_code == 400)

# 0-indexed page in reorder/extract-pages must fail instead of wrapping to last page
zero_reorder = requests.post(f"{BASE}/pdf/reorder", headers=auth_header(t1), json={"file_id": fid_u1, "order": [0, 1, 2]})
zr_res = wait_job(t1, zero_reorder.json()["job_id"])
check("reorder with 0-indexed page fails cleanly (no negative index wrap)", zr_res["status"] == "failed")

zero_extract = requests.post(f"{BASE}/pdf/extract-pages", headers=auth_header(t1), json={"file_id": fid_u1, "pages": [0]})
ze_res = wait_job(t1, zero_extract.json()["job_id"])
check("extract-pages with page 0 fails cleanly", ze_res["status"] == "failed")

# RGBA transparent PNG in images-to-pdf
rgba_buf = io.BytesIO()
rgba_img = Image.new("RGBA", (120, 120), (255, 0, 0, 128))
rgba_img.save(rgba_buf, format="PNG")
fid_rgba = upload(t1, "transparent.png", rgba_buf.getvalue()).json()["file_id"]
r_rgba = requests.post(f"{BASE}/convert/images-to-pdf", headers=auth_header(t1), json={"file_ids": [fid_rgba]})
res_rgba = wait_job(t1, r_rgba.json()["job_id"])
check("RGBA transparent PNG converts to PDF without AlphaChannelError", res_rgba["status"] == "done", str(res_rgba))

# Stop-word-only / short sentence PDF in AI summarize & ask
fid_stop = upload(t1, "stop.pdf", make_pdf_bytes(["We are in it to do it."])).json()["file_id"]
r_sum = requests.post(f"{BASE}/ai/summarize", headers=auth_header(t1), json={"file_id": fid_stop, "sentences": 1})
res_sum = wait_job(t1, r_sum.json()["job_id"])
check("AI summarize on stop-word/short sentence PDF succeeds without empty-vocab error", res_sum["status"] == "done", str(res_sum))


# ============================================================
print("=" * 60)
print("SUMMARY")
print("=" * 60)
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = [(d, det) for s, d, det in results if s == "FAIL"]
print(f"{passed}/{len(results)} checks passed")
if failed:
    for d, det in failed:
        print(f"  - {d} [{det}]")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
