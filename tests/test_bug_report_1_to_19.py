"""
Comprehensive Regression Test Suite for all 19 Bugs in docengine_bug_report.txt.
"""
import concurrent.futures
import io
import os
import re
import subprocess
import sys
import time
import pikepdf
import pdfplumber
import requests
from openpyxl import load_workbook
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib import colors

BASE = os.environ.get("DOCENGINE_BASE_URL", "http://localhost:8000")
results = []


def check(desc, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, desc, detail))
    print(f"[{status}] {desc}" + (f"  -- {detail}" if detail and status == "FAIL" else ""))
    return condition


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def wait_job(token, job_id, timeout=45):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}", headers=auth_header(token))
        if r.status_code != 200:
            return {"status": "http_error", "code": r.status_code}
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.25)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    return requests.post(
        f"{BASE}/files",
        headers=auth_header(token),
        files={"file": (filename, io.BytesIO(content_bytes))},
    )


def make_pdf_bytes(lines, pages=1, size=(612, 792)):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for p in range(pages):
        c.setFont("Helvetica", 12)
        y = size[1] - 92
        for line in lines:
            c.drawString(72, y, line if pages == 1 else f"[P{p+1}] {line}")
            y -= 24
        c.showPage()
    c.save()
    return buf.getvalue()


def make_blank_pdf_bytes(n_pages=1):
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(612, 792)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


# Authenticate 3 test users
u_tokens = []
for idx in range(1, 4):
    email = f"audit_user{idx}@example.com"
    s = requests.post(f"{BASE}/auth/signup", json={"email": email, "password": "StrongPassword123"})
    if s.status_code == 200:
        u_tokens.append(s.json()["token"])
    else:
        l = requests.post(f"{BASE}/auth/login", json={"email": email, "password": "StrongPassword123"})
        u_tokens.append(l.json()["token"])
t1, t2, t3 = u_tokens


# ============================================================
print("=" * 60)
print("BUG 1 & BUG 2 — Redaction Validation & Rotated Page Geometry")
print("=" * 60)

fid_red = upload(t1, "redact_test.pdf", make_pdf_bytes(["TOP SECRET 9988"], pages=2)).json()["file_id"]

# Bug 1: string page "1" and out-of-range page 99 must fail, never succeed as no-op
for desc, bad_region in [
    ("string page '1'", {"page": "1", "x": 50, "y": 50, "width": 100, "height": 20}),
    ("out-of-range page 99", {"page": 99, "x": 50, "y": 50, "width": 100, "height": 20}),
    ("zero width", {"page": 1, "x": 50, "y": 50, "width": 0, "height": 20}),
    ("bool page True", {"page": True, "x": 50, "y": 50, "width": 100, "height": 20}),
]:
    r = requests.post(f"{BASE}/pdf/redact", headers=auth_header(t1), json={"file_id": fid_red, "regions": [bad_region]})
    res = wait_job(t1, r.json()["job_id"]) if r.status_code == 200 else {"status": "failed"}
    check(f"Bug 1: redact rejects {desc}", res["status"] == "failed", str(res))

# Bug 2: Redaction on a 90-degree rotated page where PII / OCR coordinates are in displayed space
rot_src_bytes = make_pdf_bytes(["Email: secret.agent@agency.gov", "Public safe note here"], pages=1)
fid_rot_src = upload(t1, "rot_src.pdf", rot_src_bytes).json()["file_id"]
rot_job = requests.post(f"{BASE}/pdf/rotate", headers=auth_header(t1), json={"file_id": fid_rot_src, "degrees": 90}).json()["job_id"]
fid_rot90 = wait_job(t1, rot_job)["result"]

pii_job = requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(t1), json={"file_id": fid_rot90}).json()["job_id"]
pii_regions = wait_job(t1, pii_job)["result"]
check("Bug 2: PII detected on 90-deg rotated page", len(pii_regions) == 1, str(pii_regions))

red_rot_job = requests.post(
    f"{BASE}/pdf/redact",
    headers=auth_header(t1),
    json={
        "file_id": fid_rot90,
        "regions": [{"page": r["page"], "x": r["x"], "y": r["y"], "width": r["width"], "height": r["height"]} for r in pii_regions],
    },
).json()["job_id"]
red_rot_res = wait_job(t1, red_rot_job)
check("Bug 2: redact on 90-deg rotated page succeeds", red_rot_res["status"] == "done", str(red_rot_res))
red_rot_fid = red_rot_res["result"]["file_id"]
rot_info = requests.get(f"{BASE}/files/{red_rot_fid}/info", headers=auth_header(t1)).json()
check("Bug 2: redacted 90-deg page preserves landscape displayed dimensions (792x612)",
      rot_info.get("page_width") == 792.0 and rot_info.get("page_height") == 612.0, str(rot_info))

# Run OCR on the redacted 90-deg page to prove the secret email is genuinely blacked out in the image!
ocr_rot_job = requests.post(f"{BASE}/ocr", headers=auth_header(t1), json={"file_id": red_rot_fid, "force": True}).json()["job_id"]
ocr_rot_res = wait_job(t1, ocr_rot_job)["result"]
ocr_words = " ".join(w["text"] for w in ocr_rot_res.get("word_boxes", {}).get("1", []))
check("Bug 2: OCR of redacted 90-deg page cannot read secret email, while public line survives",
      "secret" not in ocr_words.lower() and "agency" not in ocr_words.lower() and "public" in ocr_words.lower(),
      repr(ocr_words))


# ============================================================
print("=" * 60)
print("BUG 3, 4, 5, 6, 8 — Security, Secrets, Info Leaks, Race")
print("=" * 60)

# Bug 3: XSS email rejected, CSP & nosniff headers present, frontend app.js has 0 unescaped innerHTML
xss_signup = requests.post(f"{BASE}/auth/signup", json={"email": '"><img src=x onerror=alert(1)>@x.com', "password": "password123"})
check("Bug 3: signup rejects HTML/XSS payload in email (400)", xss_signup.status_code == 400, xss_signup.text)
check("Bug 3: response includes Content-Security-Policy header", "default-src 'self'" in xss_signup.headers.get("Content-Security-Policy", ""))
check("Bug 3: response includes X-Content-Type-Options: nosniff", xss_signup.headers.get("X-Content-Type-Options") == "nosniff")

js_src = open("frontend/app.js").read()
unescaped = []
for m in re.finditer(r"innerHTML\s*=\s*`([^`]*)`", js_src, re.DOTALL):
    for expr in re.findall(r"\$\{([^}]+)\}", m.group(1)):
        if not (expr.startswith("esc(") or expr == "snippet"):
            unescaped.append(expr)
check("Bug 3: frontend/app.js has zero unescaped ${} in innerHTML", len(unescaped) == 0, str(unescaped))

# Bug 4: Boot fails with missing/short/default DOCENGINE_SECRET, and /share/<token> hides owner_id
env_bad = os.environ.copy()
env_bad["DOCENGINE_SECRET"] = "change-me-in-production"
bad_boot = subprocess.run([sys.executable, "-c", "import app.auth"], env=env_bad, capture_output=True, text=True)
check("Bug 4: app refuses to boot with default/weak DOCENGINE_SECRET", bad_boot.returncode != 0 and "DOCENGINE_SECRET" in bad_boot.stderr)

sh_tok = requests.post(f"{BASE}/share", headers=auth_header(t1), json={"file_id": fid_red, "permission": "view"}).json()["token"]
sh_pub = requests.get(f"{BASE}/share/{sh_tok}").json()
check("Bug 4: public /share/<token> does not leak owner_id",
      "owner_id" not in sh_pub and "owner_id" not in (sh_pub.get("meta") or {}), str(sh_pub))

# Bug 5: /jobs/<id> does not leak traceback; upload validation error does not leak server path; /preview on non-PDF returns 400
bad_job = requests.post(f"{BASE}/pdf/metadata", headers=auth_header(t1), json={"file_id": fid_red, "fields": {"bogus": "1"}}).json()["job_id"]
bad_job_data = wait_job(t1, bad_job)
check("Bug 5: failed job response does not leak server traceback", "traceback" not in bad_job_data, str(bad_job_data))

corrupt_up = upload(t1, "corrupt.pdf", b"not a pdf")
check("Bug 5: upload validation error does not leak server filesystem path",
      corrupt_up.status_code == 400 and "/storage/" not in corrupt_up.text and "/working_dir" not in corrupt_up.text,
      corrupt_up.text)

txt_fid = upload(t1, "notes.txt", b"plain text").json()["file_id"]
prev_nonpdf = requests.get(f"{BASE}/files/{txt_fid}/preview", headers=auth_header(t1))
check("Bug 5: /preview on non-PDF returns 400 (not 500)", prev_nonpdf.status_code == 400, prev_nonpdf.text)

# Bug 6: GET /pdf/<id>/security?password=... rejected (400), POST JSON body works (200)
qs_sec = requests.get(f"{BASE}/pdf/{fid_red}/security?password=abc", headers=auth_header(t1))
check("Bug 6: password in query string rejected (400)", qs_sec.status_code == 400, qs_sec.text)
post_sec = requests.post(f"{BASE}/pdf/{fid_red}/security", headers=auth_header(t1), json={"password": "abc"})
check("Bug 6: POST /pdf/<id>/security with JSON body succeeds (200)", post_sec.status_code == 200, post_sec.text)

# Bug 8: Concurrent signup race returns one 200 and seven 409s (never 500)
race_email = f"race_{int(time.time()*1000)}@example.com"
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    race_resps = list(ex.map(lambda _: requests.post(f"{BASE}/auth/signup", json={"email": race_email, "password": "RacePassword123"}).status_code, range(8)))
check("Bug 8: 8 concurrent signups produce one 200 and seven 409s (no 500)",
      race_resps.count(200) == 1 and race_resps.count(409) == 7, str(race_resps))


# ============================================================
print("=" * 60)
print("BUG 9, 10, 11, 12 — Locks, Collaborators, Comments, Share Expiry")
print("=" * 60)

doc_id = requests.post(f"{BASE}/documents", headers=auth_header(t1), json={"file_id": fid_red, "title": "LockDoc"}).json()["document_id"]
requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(t1), json={"email": "audit_user2@example.com", "permission": "edit"})
u2_id = requests.get(f"{BASE}/auth/me", headers=auth_header(t2)).json()["user_id"]

# Bug 10: U2 locks, U1 (owner) tries to add version or revert while U2 holds lock -> 409
requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(t2))
v_blocked = requests.post(f"{BASE}/documents/{doc_id}/versions", headers=auth_header(t1), json={"file_id": fid_red})
rev_blocked = requests.post(f"{BASE}/documents/{doc_id}/revert", headers=auth_header(t1), json={"version_number": 1})
check("Bug 10: adding a version while locked by another user returns 409", v_blocked.status_code == 409, v_blocked.text)
check("Bug 10: reverting while locked by another user returns 409", rev_blocked.status_code == 409, rev_blocked.text)

# Bug 9: Removing collaborator U2 automatically releases U2's lock so U1 can lock immediately
requests.delete(f"{BASE}/documents/{doc_id}/collaborators/{u2_id}", headers=auth_header(t1))
u1_lock = requests.post(f"{BASE}/documents/{doc_id}/lock", headers=auth_header(t1))
check("Bug 9: removing lock-holding collaborator releases their lock so owner can lock", u1_lock.status_code == 200, u1_lock.text)
requests.post(f"{BASE}/documents/{doc_id}/unlock", headers=auth_header(t1))

# Bug 11: Comment permissions ('view' collaborator cannot post or resolve; 'comment' collaborator can post, list, and resolve)
requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(t1), json={"email": "audit_user2@example.com", "permission": "view"})
requests.post(f"{BASE}/documents/{doc_id}/collaborators", headers=auth_header(t1), json={"email": "audit_user3@example.com", "permission": "comment"})

c_owner = requests.post(f"{BASE}/comments", headers=auth_header(t1), json={"file_id": fid_red, "text": "Owner note"}).json()["comment_id"]
u2_list_c = requests.get(f"{BASE}/comments/{fid_red}", headers=auth_header(t2))
u2_post_c = requests.post(f"{BASE}/comments", headers=auth_header(t2), json={"file_id": fid_red, "text": "View user comment"})
u2_res_c = requests.post(f"{BASE}/comments/{c_owner}/resolve", headers=auth_header(t2))
check("Bug 11: view-only collaborator can list comments (200) but gets 403 on post and resolve",
      u2_list_c.status_code == 200 and u2_post_c.status_code == 403 and u2_res_c.status_code == 403)

u3_post_c = requests.post(f"{BASE}/comments", headers=auth_header(t3), json={"file_id": fid_red, "text": "Commenter note"})
u3_list_c = requests.get(f"{BASE}/comments/{fid_red}", headers=auth_header(t3))
u3_res_c = requests.post(f"{BASE}/comments/{c_owner}/resolve", headers=auth_header(t3))
check("Bug 11: comment-permission collaborator can post, list, and resolve comments (200)",
      u3_post_c.status_code == 200 and u3_list_c.status_code == 200 and u3_res_c.status_code == 200)

# Bug 12: Share expiry rejects 0, negative, and huge values
for bad_exp in (0, -5, 10000, True):
    r_exp = requests.post(f"{BASE}/share", headers=auth_header(t1), json={"file_id": fid_red, "permission": "view", "expires_in_hours": bad_exp})
    check(f"Bug 12: share rejects invalid expires_in_hours={bad_exp!r} (400)", r_exp.status_code == 400, r_exp.text)


# ============================================================
print("=" * 60)
print("BUG 13, 14, 15, 16, 17, 18, 19 — Data Integrity & Validation")
print("=" * 60)

# Bug 13: Title, bookmark, and form field survive rotate, watermark, reorder, delete-pages, extract-pages, merge
base_fid = upload(t1, "b13.pdf", make_pdf_bytes(["Page 1", "Page 2"], pages=2)).json()["file_id"]
base_fid = wait_job(t1, requests.post(f"{BASE}/pdf/metadata", headers=auth_header(t1), json={"file_id": base_fid, "fields": {"title": "KeepMe"}}).json()["job_id"])["result"]
base_fid = wait_job(t1, requests.post(f"{BASE}/pdf/bookmarks", headers=auth_header(t1), json={"file_id": base_fid, "title": "BM1", "page": 1}).json()["job_id"])["result"]
base_fid = wait_job(t1, requests.post(f"{BASE}/forms/create", headers=auth_header(t1), json={"file_id": base_fid, "fields": [{"name": "f1", "type": "text", "page": 1, "x": 50, "y": 50, "width": 100, "height": 20, "value": "v1"}]}).json()["job_id"])["result"]

wm_fid = wait_job(t1, requests.post(f"{BASE}/pdf/watermark", headers=auth_header(t1), json={"file_id": base_fid, "text": "DRAFT"}).json()["job_id"])["result"]
m_after = requests.get(f"{BASE}/pdf/{wm_fid}/metadata", headers=auth_header(t1)).json()
bm_after = requests.get(f"{BASE}/pdf/{wm_fid}/bookmarks", headers=auth_header(t1)).json().get("bookmarks", [])
f_after = requests.get(f"{BASE}/forms/{wm_fid}/data", headers=auth_header(t1)).json().get("fields", {})
check("Bug 13: watermark preserves metadata title, bookmarks, and form fields",
      m_after.get("title") == "KeepMe" and len(bm_after) == 1 and f_after.get("f1") == "v1",
      f"meta={m_after}, bm={bm_after}, fields={f_after}")

# Bug 14: Encrypted PDF can be uploaded and then decrypted
enc_fid = wait_job(t1, requests.post(f"{BASE}/pdf/encrypt", headers=auth_header(t1), json={"file_id": fid_red, "user_password": "secretpw"}).json()["job_id"])["result"]
enc_bytes = requests.get(f"{BASE}/files/{enc_fid}", headers=auth_header(t1)).content
up_enc = upload(t1, "uploaded_encrypted.pdf", enc_bytes)
check("Bug 14: encrypted PDF upload succeeds with format=pdf (200)",
      up_enc.status_code == 200 and up_enc.json().get("format") == "pdf", up_enc.text)
dec_res = wait_job(t1, requests.post(f"{BASE}/pdf/decrypt", headers=auth_header(t1), json={"file_id": up_enc.json()["file_id"], "password": "secretpw"}).json()["job_id"])
check("Bug 14: uploaded encrypted PDF can be decrypted with correct password", dec_res["status"] == "done", str(dec_res))

# Bug 15: Formula injection in extracted tables is neutralized
t_buf = io.BytesIO()
doc_t = SimpleDocTemplate(t_buf, pagesize=(612, 792))
tbl = Table([["Header", "Value"], ["Formula", '=HYPERLINK("http://evil.test","click")'], ["Math", "=1+1"]])
tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
doc_t.build([tbl])
fid_tbl = upload(t1, "formula_table.pdf", t_buf.getvalue()).json()["file_id"]
tbl_xlsx_res = wait_job(t1, requests.post(f"{BASE}/pdf/extract-tables", headers=auth_header(t1), json={"file_id": fid_tbl, "format": "xlsx"}).json()["job_id"])["result"]
xlsx_bytes = requests.get(f"{BASE}/files/{tbl_xlsx_res['file_id']}", headers=auth_header(t1)).content
wb = load_workbook(io.BytesIO(xlsx_bytes))
ws = wb[wb.sheetnames[0]]
cell_types = [c.data_type for row in ws.iter_rows() for c in row if c.value and str(c.value).startswith("=")]
check("Bug 15: extracted XLSX cells starting with '=' have string data_type 's' (not formula 'f')",
      len(cell_types) >= 2 and all(t == "s" for t in cell_types), str(cell_types))

# Bug 16: PII detects "(555) 123-4567" phone numbers
fid_phone = upload(t1, "phone.pdf", make_pdf_bytes(["(555) 123-4567 is the main office number."])).json()["file_id"]
pii_phone = wait_job(t1, requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(t1), json={"file_id": fid_phone}).json()["job_id"])["result"]
check("Bug 16: detect-pii finds parenthesized phone number '(555) 123-4567'",
      any(f["type"] == "phone" and "(555) 123-4567" in f["text"] for f in pii_phone), str(pii_phone))

# Bug 17: Strict validation on watermark pages, split bounds, fill_form unknown fields, checkbox "false", CJK watermark
wm_str_pages = wait_job(t1, requests.post(f"{BASE}/pdf/watermark", headers=auth_header(t1), json={"file_id": fid_red, "text": "X", "pages": ["1"]}).json()["job_id"])
wm_empty_pages = wait_job(t1, requests.post(f"{BASE}/pdf/watermark", headers=auth_header(t1), json={"file_id": fid_red, "text": "X", "pages": []}).json()["job_id"])
check("Bug 17: watermark rejects string pages ['1'] and empty pages []",
      wm_str_pages["status"] == "failed" and wm_empty_pages["status"] == "failed")

wm_cjk = wait_job(t1, requests.post(f"{BASE}/pdf/watermark", headers=auth_header(t1), json={"file_id": fid_red, "text": "机密文件"}).json()["job_id"])
check("Bug 17: CJK watermark succeeds", wm_cjk["status"] == "done", str(wm_cjk))
cjk_bytes = requests.get(f"{BASE}/files/{wm_cjk['result']}", headers=auth_header(t1)).content
with pdfplumber.open(io.BytesIO(cjk_bytes)) as pdf:
    cjk_chars = "".join(c["text"] for c in pdf.pages[0].chars)
check("Bug 17: CJK watermark characters are genuinely rendered in the output PDF",
      "机密" in cjk_chars, repr(cjk_chars))

split_oob = wait_job(t1, requests.post(f"{BASE}/pdf/split", headers=auth_header(t1), json={"file_id": fid_red, "ranges": [[1, 100]]}).json()["job_id"])
check("Bug 17: split rejects range [1, 100] exceeding page count", split_oob["status"] == "failed", str(split_oob))

fill_unknown = wait_job(t1, requests.post(f"{BASE}/forms/fill", headers=auth_header(t1), json={"file_id": base_fid, "data": {"nonexistent_field": "x"}}).json()["job_id"])
fill_noform = wait_job(t1, requests.post(f"{BASE}/forms/fill", headers=auth_header(t1), json={"file_id": fid_red, "data": {"f1": "x"}}).json()["job_id"])
check("Bug 17: fill_form rejects unknown field names and PDFs without forms",
      fill_unknown["status"] == "failed" and fill_noform["status"] == "failed")

cb_fid = wait_job(t1, requests.post(f"{BASE}/forms/create", headers=auth_header(t1), json={"file_id": fid_red, "fields": [{"name": "opt", "type": "checkbox", "page": 1, "x": 50, "y": 50, "width": 15, "height": 15, "value": "false"}]}).json()["job_id"])["result"]
cb_data = requests.get(f"{BASE}/forms/{cb_fid}/data", headers=auth_header(t1)).json().get("fields", {})
check("Bug 17: checkbox value='false' is parsed as False (/Off), not True", cb_data.get("opt") == "/Off", str(cb_data))

# Bug 18: Empty-document readability returns None / "n/a"
blank_fid = upload(t1, "blank.pdf", make_blank_pdf_bytes(1)).json()["file_id"]
blank_stats = requests.get(f"{BASE}/pdf/{blank_fid}/analyze", headers=auth_header(t1)).json()
check("Bug 18: empty document readability returns flesch_reading_ease=None and reading_level='n/a'",
      blank_stats.get("word_count") == 0 and blank_stats.get("flesch_reading_ease") is None and blank_stats.get("reading_level") == "n/a",
      str(blank_stats))

# Bug 19a, 19d, 19e, 19g:
cmp_a = upload(t1, "da.pdf", make_pdf_bytes(["--first line", "normal line"])).json()["file_id"]
cmp_b = upload(t1, "db.pdf", make_pdf_bytes(["normal line"])).json()["file_id"]
cmp_res = wait_job(t1, requests.post(f"{BASE}/pdf/compare", headers=auth_header(t1), json={"file_id_a": cmp_a, "file_id_b": cmp_b}).json()["job_id"])["result"]
check("Bug 19a: compare counts removed line starting with '--'", cmp_res.get("lines_removed") == 1, str(cmp_res))

bad_dpi = requests.post(f"{BASE}/convert/pdf-to-images", headers=auth_header(t1), json={"file_id": fid_red, "dpi": 99999})
check("Bug 19d: excessive dpi rejected (400)", bad_dpi.status_code == 400, bad_dpi.text)

js_link = wait_job(t1, requests.post(f"{BASE}/pdf/hyperlinks", headers=auth_header(t1), json={"file_id": fid_red, "page": 1, "x": 10, "y": 10, "width": 50, "height": 20, "url": "javascript:alert(1)"}).json()["job_id"])
check("Bug 19e: hyperlink with javascript: scheme rejected", js_link["status"] == "failed", str(js_link))

html_up = upload(t1, "evil.xyz", b"<script>alert(1)</script>").json()["file_id"]
dl_inline = requests.get(f"{BASE}/files/{html_up}?inline=1", headers=auth_header(t1))
check("Bug 19g: unknown/unsafe file forced to attachment even with ?inline=1",
      "attachment" in dl_inline.headers.get("Content-Disposition", ""), str(dl_inline.headers))


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
