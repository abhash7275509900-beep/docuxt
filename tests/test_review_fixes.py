"""
Regression test suite for DocEngine_Bug_Report.txt (BUG-01 through BUG-12).
"""
import csv
import io
import os
import sys
import time
import pikepdf
import pypdfium2 as pdfium
import requests
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

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
        d = r.json()
        if d.get("status") in ("done", "failed"):
            return d
        time.sleep(0.15)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    return requests.post(
        f"{BASE}/files",
        headers=auth_header(token),
        files={"file": (filename, io.BytesIO(content_bytes))},
    )


def make_text_pdf(lines, pagesize=(612, 792)):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pagesize)
    c.setFont("Helvetica", 14)
    y = pagesize[1] - 72
    for line in lines:
        c.drawString(72, y, line)
        y -= 36
    c.save()
    return buf.getvalue()


# Authenticate two test users
u1_email = f"review_u1_{int(time.time())}@example.com"
u2_email = f"review_u2_{int(time.time())}@example.com"
r1 = requests.post(f"{BASE}/auth/signup", json={"email": u1_email, "password": "password1234"})
r2 = requests.post(f"{BASE}/auth/signup", json={"email": u2_email, "password": "password1234"})
token_a = r1.json()["token"]
token_b = r2.json()["token"]


# ============================================================
print("=" * 60)
print("BUG-01 — Compress Never Grows File & Preserves Transparency")
print("=" * 60)

# Build PDF with blue background + 300x300 RGBA horizontal alpha gradient + 40x40 flat PNG
rgba_img = Image.new("RGBA", (300, 300))
for x in range(300):
    alpha = int(255 * (x / 299.0))  # left side x=0 is fully transparent (alpha=0), right side is opaque red
    for y in range(300):
        rgba_img.putpixel((x, y), (255, 0, 0, alpha))
rgba_buf = io.BytesIO()
rgba_img.save(rgba_buf, format="PNG")
rgba_buf.seek(0)

tiny_img = Image.new("RGB", (40, 40), (20, 180, 90))
tiny_buf = io.BytesIO()
tiny_img.save(tiny_buf, format="PNG")
tiny_buf.seek(0)

pdf_buf = io.BytesIO()
c = canvas.Canvas(pdf_buf, pagesize=(612, 792))
c.setFillColorRGB(0, 0, 1)  # pure blue background
c.rect(0, 0, 612, 792, fill=1, stroke=0)
c.drawImage(ImageReader(rgba_buf), 100, 300, width=300, height=300, mask="auto")
c.drawImage(ImageReader(tiny_buf), 450, 300, width=40, height=40, mask="auto")
c.save()
alpha_pdf_bytes = pdf_buf.getvalue()

fid_alpha = upload(token_a, "alpha_logo.pdf", alpha_pdf_bytes).json()["file_id"]
for q in ("low", "medium", "high"):
    rj = requests.post(f"{BASE}/pdf/compress", headers=auth_header(token_a), json={"file_id": fid_alpha, "quality": q})
    res = wait_job(token_a, rj.json()["job_id"])
    out = res.get("result") or {}
    check(
        f"BUG-01: compress[{q}] never enlarges file (size_after <= size_before)",
        res["status"] == "done" and out.get("size_after", 1e9) <= out.get("size_before", 0) and out.get("ratio", -1) >= 0,
        str(out),
    )
    if q == "high" and out.get("file_id"):
        comp_bytes = requests.get(f"{BASE}/files/{out['file_id']}", headers=auth_header(token_a)).content
        doc = pdfium.PdfDocument(comp_bytes)
        pil_page = doc[0].render(scale=1.0).to_pil()
        doc.close()
        # Sample left side of the 300x300 RGBA image at PDF (110, 450) -> PIL (110, 792 - 450 = 342)
        # Because alpha ~ 0 at x=10, the blue background (0, 0, 255) must show through!
        r_px, g_px, b_px = pil_page.getpixel((110, 342))[:3]
        check(
            "BUG-01: compress[high] preserves RGBA alpha transparency over blue background",
            b_px > 180 and r_px < 60,
            f"pixel=(R={r_px}, G={g_px}, B={b_px})",
        )


# ============================================================
print("=" * 60)
print("BUG-02 & BUG-03 — Rotated Page PII Detection, Redaction & /info Dimensions")
print("=" * 60)

base_pii_pdf = make_text_pdf([
    "SSN 123-45-6789",
    "Email jane@example.com",
    "Card 4111 1111 1111 1111",
    "Public safe statement",
])
fid_base_pii = upload(token_a, "pii_rot_base.pdf", base_pii_pdf).json()["file_id"]

for deg in (0, 90, 180, 270):
    if deg == 0:
        rot_fid = fid_base_pii
    else:
        r_rot = requests.post(
            f"{BASE}/pdf/rotate",
            headers=auth_header(token_a),
            json={"file_id": fid_base_pii, "degrees": deg},
        )
        rot_fid = wait_job(token_a, r_rot.json()["job_id"])["result"]

    info = requests.get(f"{BASE}/files/{rot_fid}/info", headers=auth_header(token_a)).json()
    prev_png = requests.get(f"{BASE}/files/{rot_fid}/preview?page=1&dpi=72", headers=auth_header(token_a)).content
    prev_img = Image.open(io.BytesIO(prev_png))
    check(
        f"BUG-03: /info dimensions match /preview at {deg} deg ({info.get('page_width')}x{info.get('page_height')})",
        int(round(info.get("page_width", 0))) == prev_img.width
        and int(round(info.get("page_height", 0))) == prev_img.height,
        f"info={info.get('page_width')}x{info.get('page_height')} vs preview={prev_img.size}",
    )

    r_pii = requests.post(f"{BASE}/ai/detect-pii", headers=auth_header(token_a), json={"file_id": rot_fid})
    pii_res = wait_job(token_a, r_pii.json()["job_id"])
    findings = pii_res.get("result") or []
    found_types = {f["type"] for f in findings}
    check(
        f"BUG-02: detect-pii at {deg} deg finds all 3 PII types (ssn, email, credit_card)",
        found_types == {"ssn", "email", "credit_card"},
        str(findings),
    )

    # Redact all detected PII regions and verify via OCR(force=True) that none survive
    regions = [
        {"page": f["page"], "x": f["x"], "y": f["y"], "width": f["width"], "height": f["height"]}
        for f in findings
    ]
    r_red = requests.post(
        f"{BASE}/pdf/redact",
        headers=auth_header(token_a),
        json={"file_id": rot_fid, "regions": regions},
    )
    red_res = wait_job(token_a, r_red.json()["job_id"])
    red_fid = (red_res.get("result") or {}).get("file_id")
    r_ocr = requests.post(
        f"{BASE}/ocr",
        headers=auth_header(token_a),
        json={"file_id": red_fid, "force": True, "dpi": 200},
    )
    ocr_res = wait_job(token_a, r_ocr.json()["job_id"])
    ocr_words = " ".join(
        w["text"] for w in ((ocr_res.get("result") or {}).get("word_boxes") or {}).get("1", [])
    )
    check(
        f"BUG-02: redacted {deg}-deg page has zero surviving PII under forced OCR",
        "123-45-6789" not in ocr_words and "jane@example.com" not in ocr_words and "4111" not in ocr_words,
        f"ocr_words={ocr_words!r}",
    )


# ============================================================
print("=" * 60)
print("BUG-04 — Annotation Schema Validation, PATCH & Resilient Export")
print("=" * 60)

fid_ann = upload(token_a, "ann_val.pdf", make_text_pdf(["Annotation validation page"])).json()["file_id"]

bad_ann_cases = [
    ("empty highlight data {}", "highlight", 1, {}),
    ("invalid color string", "highlight", 1, {"x": 10, "y": 10, "width": 50, "height": 20, "color": "notacolor"}),
    ("freehand non-list points", "freehand", 1, {"points": "abc"}),
    ("out-of-range page 99", "comment", 99, {"x": 50, "y": 50}),
    ("unsupported emoji in stamp", "stamp", 1, {"x": 50, "y": 50, "text": "Hello \U0001f600"}),
]
for label, atype, apage, adata in bad_ann_cases:
    r_bad = requests.post(
        f"{BASE}/annotations",
        headers=auth_header(token_a),
        json={"file_id": fid_ann, "type": atype, "page": apage, "data": adata},
    )
    check(f"BUG-04: invalid annotation rejected (400): {label}", r_bad.status_code == 400, r_bad.text)

# Create valid annotation and update via PATCH
r_good = requests.post(
    f"{BASE}/annotations",
    headers=auth_header(token_a),
    json={"file_id": fid_ann, "type": "stamp", "page": 1, "data": {"x": 100, "y": 500, "text": "DRAFT"}},
)
ann_id = r_good.json()["annotation_id"]
r_patch = requests.patch(
    f"{BASE}/annotations/{ann_id}",
    headers=auth_header(token_a),
    json={"data": {"x": 100, "y": 500, "text": "FINAL"}},
)
check("BUG-04: PATCH /annotations/<id> updates annotation (200)", r_patch.status_code == 200, r_patch.text)


# ============================================================
print("=" * 60)
print("BUG-05 & BUG-06 — Wrong-Typed IDs (400) & Path-Free Job Errors")
print("=" * 60)

r_list_id = requests.post(f"{BASE}/pdf/rotate", headers=auth_header(token_a), json={"file_id": [1]})
check("BUG-05: list as file_id returns 400 (not 500)", r_list_id.status_code == 400, r_list_id.text)

r_dict_id = requests.post(f"{BASE}/pdf/rotate", headers=auth_header(token_a), json={"file_id": {"a": 1}})
check("BUG-05: dict as file_id returns 400 (not 500)", r_dict_id.status_code == 400, r_dict_id.text)

r_merge_bad = requests.post(f"{BASE}/pdf/merge", headers=auth_header(token_a), json={"file_ids": [[1], {"a": 1}]})
check("BUG-05: list/dict in file_ids returns 400 (not 500)", r_merge_bad.status_code == 400, r_merge_bad.text)

fid_txt = upload(token_a, "plain.txt", b"Plain text file, not a PDF").json()["file_id"]
r_comp_txt = requests.post(f"{BASE}/pdf/compress", headers=auth_header(token_a), json={"file_id": fid_txt})
comp_txt_job = wait_job(token_a, r_comp_txt.json()["job_id"])
err_str = comp_txt_job.get("error") or ""
check(
    "BUG-06: failed job error contains no server filesystem path",
    comp_txt_job["status"] == "failed"
    and "/storage/" not in err_str
    and "/working_dir" not in err_str
    and "/tmp/" not in err_str,
    f"error={err_str!r}",
)


# ============================================================
print("=" * 60)
print("BUG-07 & BUG-08 — Public Share Comment Privacy & Logout UI Reset")
print("=" * 60)

requests.post(
    f"{BASE}/comments",
    headers=auth_header(token_a),
    json={"file_id": fid_ann, "text": "Internal review note", "mentions": ["colleague@secretcorp.com"]},
)
sh_tok = requests.post(
    f"{BASE}/share",
    headers=auth_header(token_a),
    json={"file_id": fid_ann, "permission": "comment"},
).json()["token"]
requests.post(
    f"{BASE}/share/{sh_tok}/comments",
    json={"author_name": "Admin", "text": "External comment"},
)

pub_comments_resp = requests.get(f"{BASE}/share/{sh_tok}/comments")
pub_comments = pub_comments_resp.json().get("comments", [])
check(
    "BUG-07: public /share/<token>/comments hides author_id and raw emails",
    len(pub_comments) == 2
    and all("author_id" not in c for c in pub_comments)
    and "@" not in pub_comments_resp.text
    and any(c.get("is_guest") is True and "(guest)" in c.get("author_label", "") for c in pub_comments),
    pub_comments_resp.text,
)

with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "app.js")) as f:
    app_js = f.read()
check(
    "BUG-08: frontend resets full state, hides canvas, and revokes blob URLs on logout/401",
    "function resetSessionAndUI()" in app_js
    and "URL.revokeObjectURL" in app_js
    and 'removeAttribute("src")' in app_js,
)


# ============================================================
print("=" * 60)
print("BUG-09 & BUG-10 — CSV Signed Numbers & Upload Validation")
print("=" * 60)

tbl_buf = io.BytesIO()
doc = SimpleDocTemplate(tbl_buf, pagesize=(612, 792))
t = Table([
    ["Metric", "Delta", "Formula"],
    ["Revenue", "-12.5", "=SUM(A1:A2)"],
    ["Growth", "+3", "+A1+B1"],
])
t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
doc.build([t])
fid_num_tbl = upload(token_a, "numbers.pdf", tbl_buf.getvalue()).json()["file_id"]

r_csv = requests.post(
    f"{BASE}/pdf/extract-tables",
    headers=auth_header(token_a),
    json={"file_id": fid_num_tbl, "format": "csv"},
)
csv_out = wait_job(token_a, r_csv.json()["job_id"])["result"]
csv_text = requests.get(f"{BASE}/files/{csv_out['file_id']}", headers=auth_header(token_a)).text
csv_rows = list(csv.reader(io.StringIO(csv_text)))
check(
    "BUG-09: CSV preserves '-12.5' and '+3' without leading quote while quoting formulas",
    any("-12.5" in r and "'=SUM(A1:A2)" in r for r in csv_rows)
    and any("+3" in r and "'+A1+B1" in r for r in csv_rows),
    f"rows={csv_rows}",
)

r_junk_tif = upload(token_a, "x.tif", b"junk")
check("BUG-10: upload rejects junk .tif file (400)", r_junk_tif.status_code == 400, r_junk_tif.text)

r_junk_doc = upload(token_a, "bad.doc", b"junk")
check("BUG-10: upload rejects junk .doc file (400)", r_junk_doc.status_code == 400, r_junk_doc.text)

r_unknown_ext = upload(token_a, "malware.exe", b"MZ\x90\x00")
check("BUG-10: upload rejects unsupported extension .exe (400)", r_unknown_ext.status_code == 400, r_unknown_ext.text)

r_no_ext = upload(token_a, "noextension", b"hello")
check("BUG-10: upload rejects filename with no extension (400)", r_no_ext.status_code == 400, r_no_ext.text)


# ============================================================
print("=" * 60)
print("BUG-11 & BUG-12 — Owner-Only Encryption, Password Cap, Batch, Metadata, Delete, Convert & Checkbox")
print("=" * 60)

# 11a: Owner-only-password PDF reports pages in /info
r_own_enc = requests.post(
    f"{BASE}/pdf/encrypt",
    headers=auth_header(token_a),
    json={"file_id": fid_ann, "user_password": "", "owner_password": "owner-secret-pw"},
)
own_enc_fid = wait_job(token_a, r_own_enc.json()["job_id"])["result"]
own_info = requests.get(f"{BASE}/files/{own_enc_fid}/info", headers=auth_header(token_a)).json()
check(
    "BUG-11a: owner-only-password PDF reports pages=1 and requires_password=False in /info",
    own_info.get("pages") == 1 and own_info.get("requires_password") is False,
    str(own_info),
)

# 11c: Password > 128 chars rejected on signup and login
r_long_pw = requests.post(
    f"{BASE}/auth/signup",
    json={"email": "longpw@example.com", "password": "a" * 500},
)
check("BUG-11c: signup rejects password > 128 chars (400)", r_long_pw.status_code == 400, r_long_pw.text)

r_long_login = requests.post(
    f"{BASE}/auth/login",
    json={"email": u1_email, "password": "a" * 500},
)
check("BUG-11c: login rejects password > 128 chars (400)", r_long_login.status_code == 400, r_long_login.text)

# 11d: Batch rejects unknown params
r_bad_batch = requests.post(
    f"{BASE}/batch",
    headers=auth_header(token_a),
    json={"operation": "rotate", "file_ids": [fid_ann], "params": {"foo": 1}},
)
check("BUG-11d: batch rejects unknown parameter 'foo' with 400", r_bad_batch.status_code == 400, r_bad_batch.text)

# 11e: Metadata rejects list fields
r_bad_meta = requests.post(
    f"{BASE}/pdf/metadata",
    headers=auth_header(token_a),
    json={"file_id": fid_ann, "fields": [1, 2]},
)
check("BUG-11e: /pdf/metadata rejects non-dict fields with 400", r_bad_meta.status_code == 400, r_bad_meta.text)

# 12b: PDF -> PPTX rejected with 400; PDF -> XLSX succeeds
r_pdf_pptx = requests.post(
    f"{BASE}/convert/office",
    headers=auth_header(token_a),
    json={"file_id": fid_num_tbl, "target_format": "pptx"},
)
check("BUG-12b: incompatible conversion PDF -> PPTX rejected (400)", r_pdf_pptx.status_code == 400, r_pdf_pptx.text)

r_pdf_xlsx = requests.post(
    f"{BASE}/convert/office",
    headers=auth_header(token_a),
    json={"file_id": fid_num_tbl, "target_format": "xlsx"},
)
pdf_xlsx_job = wait_job(token_a, r_pdf_xlsx.json()["job_id"])
check("BUG-12b: PDF -> XLSX conversion succeeds via table extraction", pdf_xlsx_job["status"] == "done", str(pdf_xlsx_job))

# 12c: Checkbox with custom /On state in /AP /N is ticked properly by fill_form
with pikepdf.open(io.BytesIO(make_text_pdf(["Custom checkbox test"]))) as pdf:
    widget = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Widget"),
        "/FT": pikepdf.Name("/Btn"),
        "/T": "custom_cb",
        "/Rect": pikepdf.Array([100, 500, 120, 520]),
        "/V": pikepdf.Name("/Off"),
        "/AS": pikepdf.Name("/Off"),
        "/AP": pikepdf.Dictionary({
            "/N": pikepdf.Dictionary({
                "/On": pikepdf.Stream(pdf, b"q 0 g 0 0 20 20 re f Q"),
                "/Off": pikepdf.Stream(pdf, b""),
            })
        }),
    }))
    pdf.pages[0]["/Annots"] = pikepdf.Array([widget])
    pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary({
        "/Fields": pikepdf.Array([widget]),
    }))
    cb_buf = io.BytesIO()
    pdf.save(cb_buf)

fid_cb = upload(token_a, "custom_cb.pdf", cb_buf.getvalue()).json()["file_id"]
r_fill_cb = requests.post(
    f"{BASE}/forms/fill",
    headers=auth_header(token_a),
    json={"file_id": fid_cb, "data": {"custom_cb": True}},
)
filled_cb_id = wait_job(token_a, r_fill_cb.json()["job_id"])["result"]
cb_data = requests.get(f"{BASE}/forms/{filled_cb_id}/data", headers=auth_header(token_a)).json()
check(
    "BUG-12c: fill_form uses widget's '/On' state from /AP /N when ticking checkbox",
    cb_data.get("fields", {}).get("custom_cb") == "/On",
    str(cb_data),
)

# 12a: DELETE /files/<id> removes file
fid_to_del = upload(token_a, "delete_me.pdf", make_text_pdf(["Delete me"])).json()["file_id"]
r_del_other = requests.delete(f"{BASE}/files/{fid_to_del}", headers=auth_header(token_b))
check("BUG-12a: non-owner cannot delete file (403)", r_del_other.status_code == 403, r_del_other.text)
r_del_own = requests.delete(f"{BASE}/files/{fid_to_del}", headers=auth_header(token_a))
r_after_del = requests.get(f"{BASE}/files/{fid_to_del}", headers=auth_header(token_a))
check(
    "BUG-12a: owner DELETE /files/<id> succeeds (200) and file is gone (404)",
    r_del_own.status_code == 200 and r_after_del.status_code == 404,
    f"del={r_del_own.status_code} get={r_after_del.status_code}",
)


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
