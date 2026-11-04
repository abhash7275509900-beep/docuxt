import os
"""
Regression test — Phase 8 Part E (batch operations).
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


def make_photo_pdf_bytes(seed):
    import img2pdf
    from PIL import Image
    import random
    random.seed(seed)
    img = Image.new("RGB", (600, 500))
    img.putdata([(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                 for _ in range(600 * 500)])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return img2pdf.convert(buf.getvalue())


def make_text_pdf_bytes(marker):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.drawString(72, 700, f"Document content marker {marker}")
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase8e_test@test.com", "password": "testpass12"})
token = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase8e_test@test.com", "password": "testpass12"}).json()["token"]

signup2 = requests.post(f"{BASE}/auth/signup",
                         json={"email": "phase8e_other@test.com", "password": "testpass12"})
token_other = signup2.json()["token"] if signup2.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase8e_other@test.com", "password": "testpass12"}).json()["token"]

check("both test users authenticated", bool(token) and bool(token_other))


# ============================================================
print("=" * 60)
print("Batch compress")
print("=" * 60)

fids = [upload(token, f"photo{i}.pdf", make_photo_pdf_bytes(100 + i)).json()["file_id"]
        for i in range(3)]
orig_sizes = {fid: requests.get(f"{BASE}/files/{fid}/info", headers=auth_header(token)).json()["size"]
              for fid in fids}

r = requests.post(f"{BASE}/batch", headers=auth_header(token),
                   json={"operation": "compress", "file_ids": fids, "params": {"quality": "high"}})
check("batch compress request succeeds", r.status_code == 200, r.text)
job_ids = r.json().get("job_ids", {})
check("batch returns one job_id per file", set(job_ids.keys()) == set(fids), str(job_ids))

all_compressed_ok = True
for fid, job_id in job_ids.items():
    result = wait_job(token, job_id)
    out = result.get("result") or {}
    if result["status"] != "done" or out.get("size_after", 1e9) >= orig_sizes[fid]:
        all_compressed_ok = False
check("every file in the batch was independently compressed (smaller than its own original)",
      all_compressed_ok)


# ============================================================
print("=" * 60)
print("Batch watermark")
print("=" * 60)

fid_a = upload(token, "doc_a.pdf", make_text_pdf_bytes("ALPHA999")).json()["file_id"]
fid_b = upload(token, "doc_b.pdf", make_text_pdf_bytes("BETA888")).json()["file_id"]

r = requests.post(f"{BASE}/batch", headers=auth_header(token),
                   json={"operation": "watermark", "file_ids": [fid_a, fid_b],
                         "params": {"text": "BATCH CONFIDENTIAL", "opacity": 0.3}})
check("batch watermark request succeeds", r.status_code == 200, r.text)
job_ids2 = r.json().get("job_ids", {})

import pdfplumber
markers = {fid_a: "ALPHA999", fid_b: "BETA888"}
all_watermarked_ok = True
for fid, job_id in job_ids2.items():
    result = wait_job(token, job_id)
    wm_id = result.get("result")
    if not wm_id:
        all_watermarked_ok = False
        continue
    content = requests.get(f"{BASE}/files/{wm_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        chars = "".join(c["text"] for c in pdf.pages[0].chars)
    if not all(ch in chars for ch in "BATCH") or markers[fid] not in chars:
        all_watermarked_ok = False
check("every file kept its own distinct content AND got the shared watermark",
      all_watermarked_ok)


# ============================================================
print("=" * 60)
print("Batch — error handling")
print("=" * 60)

fid_owned = upload(token, "owned.pdf", make_text_pdf_bytes("OWNED")).json()["file_id"]
fid_not_owned = upload(token_other, "notowned.pdf", make_text_pdf_bytes("NOTOWNED")).json()["file_id"]

r = requests.post(f"{BASE}/batch", headers=auth_header(token),
                   json={"operation": "compress", "file_ids": [fid_owned, fid_not_owned],
                         "params": {"quality": "medium"}})
check("batch with one non-owned file is rejected entirely (403), not partially run",
      r.status_code == 403, r.text)

r2 = requests.post(f"{BASE}/batch", headers=auth_header(token),
                    json={"operation": "delete_everything", "file_ids": [fid_owned], "params": {}})
check("unknown batch operation name rejected (400)", r2.status_code == 400, r2.text)

r3 = requests.post(f"{BASE}/batch", headers=auth_header(token),
                    json={"operation": "rotate", "file_ids": ["doesnotexist.pdf"], "params": {"degrees": 90}})
check("batch with a genuinely nonexistent file_id gives 404", r3.status_code == 404, r3.text)


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
