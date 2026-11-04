import os
"""
Regression test — Phase 0, 1, 2 only.
Prints PASS/FAIL per check and a final summary. Non-zero exit if any failed.
"""
import sys
import time
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
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}", headers=auth_header(token))
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.3)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    r = requests.post(f"{BASE}/files", headers=auth_header(token),
                       files={"file": (filename, io.BytesIO(content_bytes))})
    return r


def make_pdf_bytes(text_lines, size=(612, 792)):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.setFont("Helvetica", 14)
    y = size[1] - 100
    for line in text_lines:
        c.drawString(72, y, line)
        y -= 30
    c.save()
    return buf.getvalue()


def make_blank_pdf_bytes(n_pages=1, size=(612, 792)):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=size[0], height=size[1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ============================================================
print("=" * 60)
print("PHASE 0 — Auth, ownership, metadata")
print("=" * 60)

r = requests.get(f"{BASE}/health")
check("health endpoint responds", r.status_code == 200)

r = requests.post(f"{BASE}/files")
check("unauthenticated upload rejected (401)", r.status_code == 401)

signup_a = requests.post(f"{BASE}/auth/signup",
                          json={"email": "alice_p02@test.com", "password": "hunter2222"})
check("signup succeeds", signup_a.status_code == 200, signup_a.text)
token_a = signup_a.json().get("token")

dup = requests.post(f"{BASE}/auth/signup",
                     json={"email": "alice_p02@test.com", "password": "hunter2222"})
check("duplicate signup rejected (409)", dup.status_code == 409, dup.text)

bad_login = requests.post(f"{BASE}/auth/login",
                           json={"email": "alice_p02@test.com", "password": "wrong"})
check("wrong password rejected (401)", bad_login.status_code == 401)

good_login = requests.post(f"{BASE}/auth/login",
                            json={"email": "alice_p02@test.com", "password": "hunter2222"})
check("correct login succeeds", good_login.status_code == 200)

me = requests.get(f"{BASE}/auth/me", headers=auth_header(token_a))
check("bearer token authenticates /auth/me", me.status_code == 200 and
      me.json().get("email") == "alice_p02@test.com", me.text)

bad_token = requests.get(f"{BASE}/auth/me", headers=auth_header("garbage.token.value"))
check("garbage token rejected (401)", bad_token.status_code == 401)

short_pw = requests.post(f"{BASE}/auth/signup",
                          json={"email": "shortpw@test.com", "password": "abc"})
check("signup with too-short password rejected (400)", short_pw.status_code == 400)

signup_b = requests.post(f"{BASE}/auth/signup",
                          json={"email": "bob_p02@test.com", "password": "passw0rd2"})
check("second user signup succeeds", signup_b.status_code == 200, signup_b.text)
token_b = signup_b.json().get("token")

pdf_bytes = make_blank_pdf_bytes(4)
up = upload(token_a, "doc.pdf", pdf_bytes)
check("alice can upload a valid pdf", up.status_code == 200, up.text)
fid_main = up.json().get("file_id")
check("upload response includes detected format=pdf", up.json().get("format") == "pdf")

listing = requests.get(f"{BASE}/files", headers=auth_header(token_a))
check("alice's file list contains her upload",
      any(f["id"] == fid_main for f in listing.json()["files"]))

bob_listing = requests.get(f"{BASE}/files", headers=auth_header(token_b))
check("bob's file list is empty (isolation)", bob_listing.json()["files"] == [])

bob_dl = requests.get(f"{BASE}/files/{fid_main}", headers=auth_header(token_b))
check("bob downloading alice's file is rejected (403)", bob_dl.status_code == 403)

info = requests.get(f"{BASE}/files/{fid_main}/info", headers=auth_header(token_a))
check("file info reports correct page count", info.json().get("pages") == 4, info.text)

nonexistent = requests.get(f"{BASE}/files/doesnotexist.pdf", headers=auth_header(token_a))
check("nonexistent file_id returns 404", nonexistent.status_code == 404)


# ============================================================
print("=" * 60)
print("PHASE 1 — Core PDF operations")
print("=" * 60)

fid_a5 = upload(token_a, "a5.pdf", make_blank_pdf_bytes(5)).json()["file_id"]
fid_b3 = upload(token_a, "b3.pdf", make_blank_pdf_bytes(3)).json()["file_id"]

r = requests.post(f"{BASE}/pdf/merge", headers=auth_header(token_a),
                   json={"file_ids": [fid_a5, fid_b3]})
result = wait_job(token_a, r.json()["job_id"])
merged_id = result.get("result")
merged_info = requests.get(f"{BASE}/files/{merged_id}/info", headers=auth_header(token_a)).json()
check("merge: 5+3 pages = 8", merged_info.get("pages") == 8, str(merged_info))

bob_merge = requests.post(f"{BASE}/pdf/merge", headers=auth_header(token_b),
                           json={"file_ids": [fid_a5, fid_b3]})
check("bob cannot merge alice's files (403)", bob_merge.status_code == 403)

r = requests.post(f"{BASE}/pdf/split", headers=auth_header(token_a),
                   json={"file_id": merged_id, "ranges": [[1, 2], [3, 8]]})
result = wait_job(token_a, r.json()["job_id"])
split_ids = result.get("result")
check("split: produces 2 files", isinstance(split_ids, list) and len(split_ids) == 2, str(result))
if split_ids and len(split_ids) == 2:
    p1 = requests.get(f"{BASE}/files/{split_ids[0]}/info", headers=auth_header(token_a)).json()
    p2 = requests.get(f"{BASE}/files/{split_ids[1]}/info", headers=auth_header(token_a)).json()
    check("split: page counts correct (2 and 6)", p1.get("pages") == 2 and p2.get("pages") == 6,
          f"{p1.get('pages')}, {p2.get('pages')}")

r = requests.post(f"{BASE}/pdf/reorder", headers=auth_header(token_a),
                   json={"file_id": fid_b3, "order": [3, 1, 2]})
result = wait_job(token_a, r.json()["job_id"])
check("reorder: job succeeds", result["status"] == "done", str(result))

r = requests.post(f"{BASE}/pdf/rotate", headers=auth_header(token_a),
                   json={"file_id": merged_id, "degrees": 90})
result = wait_job(token_a, r.json()["job_id"])
check("rotate: job succeeds", result["status"] == "done", str(result))

r = requests.post(f"{BASE}/pdf/delete-pages", headers=auth_header(token_a),
                   json={"file_id": merged_id, "pages": [1, 2]})
result = wait_job(token_a, r.json()["job_id"])
del_info = requests.get(f"{BASE}/files/{result.get('result')}/info", headers=auth_header(token_a)).json()
check("delete-pages: 8-2=6 pages remain", del_info.get("pages") == 6, str(del_info))

r = requests.post(f"{BASE}/pdf/extract-pages", headers=auth_header(token_a),
                   json={"file_id": merged_id, "pages": [2, 4, 6]})
result = wait_job(token_a, r.json()["job_id"])
ext_info = requests.get(f"{BASE}/files/{result.get('result')}/info", headers=auth_header(token_a)).json()
check("extract-pages: extracts exactly 3 pages", ext_info.get("pages") == 3, str(ext_info))

# compression with a real image-heavy pdf (worst-case: high-entropy noise)
import img2pdf as _i2p
from PIL import Image as _Img
import random as _rnd
_rnd.seed(3)
noisy = _Img.new("RGB", (1200, 900))
noisy.putdata([(_rnd.randint(0, 255), _rnd.randint(0, 255), _rnd.randint(0, 255)) for _ in range(1200 * 900)])
noisy_buf = io.BytesIO()
noisy.save(noisy_buf, format="JPEG", quality=95)
photo_pdf_bytes = _i2p.convert(noisy_buf.getvalue())
fid_photo = upload(token_a, "photo.pdf", photo_pdf_bytes).json()["file_id"]
orig_size = len(photo_pdf_bytes)

sizes = {}
for quality in ("low", "medium", "high"):
    r = requests.post(f"{BASE}/pdf/compress", headers=auth_header(token_a),
                       json={"file_id": fid_photo, "quality": quality})
    result = wait_job(token_a, r.json()["job_id"])
    out = result.get("result") or {}
    sizes[quality] = out.get("size_after")
    check(f"compress[{quality}]: reduces size vs original", out.get("size_after", 1e9) < orig_size,
          str(out))
if all(sizes.values()):
    check("compress: high < medium < low (monotonic aggressiveness)",
          sizes["high"] < sizes["medium"] < sizes["low"], str(sizes))

red_png_buf = io.BytesIO()
_Img.new("RGB", (200, 200), "red").save(red_png_buf, format="PNG")
fid_png = upload(token_a, "img.png", red_png_buf.getvalue()).json()["file_id"]
r = requests.post(f"{BASE}/convert/images-to-pdf", headers=auth_header(token_a),
                   json={"file_ids": [fid_png]})
result = wait_job(token_a, r.json()["job_id"])
check("images-to-pdf: produces a pdf", result["status"] == "done", str(result))

r = requests.post(f"{BASE}/convert/pdf-to-images", headers=auth_header(token_a),
                   json={"file_id": fid_a5, "fmt": "png", "dpi": 100})
result = wait_job(token_a, r.json()["job_id"])
imgs = result.get("result")
check("pdf-to-images: 5 pages -> 5 images", isinstance(imgs, list) and len(imgs) == 5, str(result))
if imgs:
    img_content = requests.get(f"{BASE}/files/{imgs[0]}", headers=auth_header(token_a)).content
    check("pdf-to-images: output is a real PNG", img_content[:8] == b'\x89PNG\r\n\x1a\n')


# ============================================================
print("=" * 60)
print("PHASE 2 — Annotations")
print("=" * 60)

fid_ann = upload(token_a, "ann.pdf", make_pdf_bytes(["Annotation test page"])).json()["file_id"]

ann_specs = [
    ("highlight", {"x": 100, "y": 650, "width": 150, "height": 15, "color": "#FFFF00"}),
    ("underline", {"x": 100, "y": 600, "width": 100, "color": "#FF0000"}),
    ("strikethrough", {"x": 100, "y": 570, "width": 100, "color": "#0000FF"}),
    ("freehand", {"points": [[50, 50], [80, 100], [120, 60]], "color": "#00AA00", "stroke_width": 2}),
    ("comment", {"x": 300, "y": 700, "text": "note", "color": "#FF00FF"}),
    ("stamp", {"x": 60, "y": 60, "text": "APPROVED", "color": "#008800"}),
    ("text_edit", {"x": 100, "y": 500, "width": 80, "height": 14, "text": "EDITED",
                    "font_size": 10, "color": "#000000", "background": "#FFFFFF"}),
]
ann_ids = []
for atype, data in ann_specs:
    r = requests.post(f"{BASE}/annotations", headers=auth_header(token_a),
                       json={"file_id": fid_ann, "type": atype, "page": 1, "data": data})
    check(f"annotation create: {atype}", r.status_code == 200, r.text)
    if r.status_code == 200:
        ann_ids.append(r.json()["annotation_id"])

bad_type = requests.post(f"{BASE}/annotations", headers=auth_header(token_a),
                          json={"file_id": fid_ann, "type": "bogus", "page": 1, "data": {}})
check("annotation invalid type rejected (400)", bad_type.status_code == 400)

listing = requests.get(f"{BASE}/annotations/{fid_ann}", headers=auth_header(token_a))
check("annotation list returns all 7 created", len(listing.json().get("annotations", [])) == 7,
      str(len(listing.json().get("annotations", []))))

bob_ann = requests.get(f"{BASE}/annotations/{fid_ann}", headers=auth_header(token_b))
check("bob cannot view alice's annotations (403)", bob_ann.status_code == 403)

if ann_ids:
    del_r = requests.delete(f"{BASE}/annotations/{ann_ids[0]}", headers=auth_header(token_a))
    check("annotation delete succeeds", del_r.status_code == 200, del_r.text)
    listing2 = requests.get(f"{BASE}/annotations/{fid_ann}", headers=auth_header(token_a))
    check("annotation count decreased after delete", len(listing2.json()["annotations"]) == 6)

bob_del = requests.delete(f"{BASE}/annotations/{ann_ids[1] if len(ann_ids) > 1 else 'x'}",
                           headers=auth_header(token_b))
check("bob cannot delete alice's annotation (404, not-yours)", bob_del.status_code == 404)

export_before_size = requests.get(f"{BASE}/files/{fid_ann}/info", headers=auth_header(token_a)).json()["size"]
r = requests.post(f"{BASE}/annotations/{fid_ann}/export", headers=auth_header(token_a))
result = wait_job(token_a, r.json()["job_id"])
exported_id = result.get("result")
check("annotation export succeeds", result["status"] == "done", str(result))
export_after_size = requests.get(f"{BASE}/files/{fid_ann}/info", headers=auth_header(token_a)).json()["size"]
check("export does not mutate the source file (non-destructive)",
      export_before_size == export_after_size)

if exported_id:
    import pdfplumber
    content = requests.get(f"{BASE}/files/{exported_id}", headers=auth_header(token_a)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        chars = "".join(c["text"] for c in pdf.pages[0].chars)
    check("exported pdf contains stamp text 'APPROVED'", "APPROVED" in chars)
    check("exported pdf contains text_edit replacement 'EDITED'", "EDITED" in chars)


# ============================================================
print("=" * 60)
print("SUMMARY")
print("=" * 60)
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = [d for s, d, det in results if s == "FAIL"]
print(f"{passed}/{len(results)} checks passed")
if failed:
    print("FAILED CHECKS:")
    for d in failed:
        print(f"  - {d}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
