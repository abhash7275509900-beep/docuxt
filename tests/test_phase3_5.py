import os
"""
Regression test — Phase 3, 4, 5.
Prints PASS/FAIL per check and a final summary. Non-zero exit if any failed.
"""
import sys
import time
import io
import requests
import pdfplumber
import pikepdf

BASE = os.environ.get("DOCENGINE_BASE_URL", "http://localhost:8000")
results = []


def check(desc, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, desc, detail))
    print(f"[{status}] {desc}" + (f"  -- {detail}" if detail and status == 'FAIL' else ""))
    return condition


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def wait_job(token, job_id, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}", headers=auth_header(token))
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.3)
    return {"status": "timeout"}


def upload(token, filename, content_bytes):
    return requests.post(f"{BASE}/files", headers=auth_header(token),
                          files={"file": (filename, io.BytesIO(content_bytes))})


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


def make_docx_bytes(marker):
    from docx import Document
    doc = Document()
    doc.add_paragraph(f"Regression test marker: {marker}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_xlsx_bytes(a1, b1, a2, b2):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = a1, b1
    ws["A2"], ws["B2"] = a2, b2
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx_bytes(title_text):
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title_text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_scanned_pdf_bytes(text, size_px=(1400, 1800), dpi=200, font_size=34):
    from PIL import Image, ImageDraw, ImageFont
    import img2pdf
    import textwrap
    img = Image.new("RGB", size_px, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    y = 100
    for line in textwrap.wrap(text, width=40):
        draw.text((100, y), line, font=font, fill="black")
        y += font_size + 12
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    return img2pdf.convert(buf.getvalue())


def make_blank_pdf_bytes(n_pages=1, size=(612, 792)):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=size[0], height=size[1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase345@test.com", "password": "testpass12"})
if signup.status_code != 200:
    login = requests.post(f"{BASE}/auth/login",
                           json={"email": "phase345@test.com", "password": "testpass12"})
    token = login.json()["token"]
else:
    token = signup.json()["token"]
check("test user authenticated", bool(token))


# ============================================================
print("=" * 60)
print("PHASE 3 — Office conversion, extraction, validation")
print("=" * 60)

r = requests.get(f"{BASE}/convert/office/formats", headers=auth_header(token))
check("office formats endpoint responds", r.status_code == 200 and len(r.json().get("formats", [])) > 0, r.text)

docx_bytes = make_docx_bytes("REGRESSION7788")
fid_docx = upload(token, "report.docx", docx_bytes).json()["file_id"]
info = requests.get(f"{BASE}/files/{fid_docx}/info", headers=auth_header(token)).json()
check("valid docx upload passes validation with format=docx",
      info.get("meta", {}).get("format") == "docx", str(info))

xlsx_bytes = make_xlsx_bytes("Quarter", "Revenue", "Q1", 42000)
fid_xlsx = upload(token, "sales.xlsx", xlsx_bytes).json()["file_id"]
check("valid xlsx upload passes validation",
      requests.get(f"{BASE}/files/{fid_xlsx}/info", headers=auth_header(token)).json()
      .get("meta", {}).get("format") == "xlsx")

pptx_bytes = make_pptx_bytes("Phase3 Slide")
fid_pptx = upload(token, "deck.pptx", pptx_bytes).json()["file_id"]
check("valid pptx upload passes validation",
      requests.get(f"{BASE}/files/{fid_pptx}/info", headers=auth_header(token)).json()
      .get("meta", {}).get("format") == "pptx")

fake_pdf = upload(token, "fake.pdf", b"not a real pdf at all")
check("fake pdf upload rejected (400)", fake_pdf.status_code == 400, fake_pdf.text)

fake_docx = upload(token, "fake.docx", make_pdf_bytes(["not a docx"]))
check("fake docx upload rejected (400)", fake_docx.status_code == 400, fake_docx.text)

unknown_ext = upload(token, "note.xyz", b"hello world")
check("unknown extension passes through as format=unknown",
      unknown_ext.status_code == 200 and unknown_ext.json().get("format") == "unknown",
      unknown_ext.text)

# docx -> pdf, verify exact text marker survives
r = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                   json={"file_id": fid_docx, "target_format": "pdf"})
result = wait_job(token, r.json()["job_id"])
check("docx->pdf conversion succeeds", result["status"] == "done", str(result))
converted_pdf_id = result.get("result")
if converted_pdf_id:
    content = requests.get(f"{BASE}/files/{converted_pdf_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    check("docx->pdf preserves exact text marker", "REGRESSION7788" in text, repr(text))

# xlsx -> pdf, verify real cell values survive
r = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                   json={"file_id": fid_xlsx, "target_format": "pdf"})
result = wait_job(token, r.json()["job_id"])
check("xlsx->pdf conversion succeeds", result["status"] == "done", str(result))
if result.get("result"):
    content = requests.get(f"{BASE}/files/{result['result']}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    check("xlsx->pdf preserves real cell values", "42000" in text and "Revenue" in text, repr(text))

# pptx -> pdf
r = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                   json={"file_id": fid_pptx, "target_format": "pdf"})
result = wait_job(token, r.json()["job_id"])
check("pptx->pdf conversion succeeds", result["status"] == "done", str(result))

# pdf -> docx -> pdf round trip (the hard direction)
src_pdf_bytes = make_pdf_bytes(["Roundtrip marker ZQVX9931"])
fid_src = upload(token, "source.pdf", src_pdf_bytes).json()["file_id"]
r = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                   json={"file_id": fid_src, "target_format": "docx"})
result = wait_job(token, r.json()["job_id"])
check("pdf->docx conversion succeeds", result["status"] == "done", str(result))
docx_id = result.get("result")
if docx_id:
    r2 = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                        json={"file_id": docx_id, "target_format": "pdf"})
    result2 = wait_job(token, r2.json()["job_id"])
    check("docx(from pdf)->pdf round trip succeeds", result2["status"] == "done", str(result2))
    if result2.get("result"):
        content = requests.get(f"{BASE}/files/{result2['result']}", headers=auth_header(token)).content
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = pdf.pages[0].extract_text() or ""
        check("pdf->docx->pdf roundtrip preserves marker", "ZQVX9931" in text, repr(text))

# concurrency: fire 3 office conversions at once
import concurrent.futures as _cf
def _convert_job():
    fid = upload(token, "r.docx", make_docx_bytes("CONC")).json()["file_id"]
    rr = requests.post(f"{BASE}/convert/office", headers=auth_header(token),
                        json={"file_id": fid, "target_format": "pdf"})
    return wait_job(token, rr.json()["job_id"])

with _cf.ThreadPoolExecutor(max_workers=3) as ex:
    conc_results = list(ex.map(lambda _: _convert_job(), range(3)))
check("3 concurrent office conversions all succeed",
      all(r["status"] == "done" for r in conc_results), str(conc_results))

# extraction
extract_src = upload(token, "extract_src.pdf",
                      make_pdf_bytes(["Extraction marker LMNO4455"])).json()["file_id"]
for fmt in ("text", "html", "markdown"):
    r = requests.post(f"{BASE}/extract/text", headers=auth_header(token),
                       json={"file_id": extract_src, "format": fmt})
    result = wait_job(token, r.json()["job_id"])
    out_id = result.get("result")
    content = requests.get(f"{BASE}/files/{out_id}", headers=auth_header(token)).content.decode()
    check(f"extract/{fmt} contains the marker", "LMNO4455" in content)

bad_fmt = requests.post(f"{BASE}/extract/text", headers=auth_header(token),
                         json={"file_id": extract_src, "format": "bogus"})
check("extract with invalid format rejected (400)", bad_fmt.status_code == 400)


# ============================================================
print("=" * 60)
print("PHASE 4 — OCR")
print("=" * 60)

scanned_bytes = make_scanned_pdf_bytes(
    "OCRMARKER8899: this scanned test line is written with enough words "
    "for Tesseract's OSD script detector to have a reasonable sample to work with."
)
with pdfplumber.open(io.BytesIO(scanned_bytes)) as pdf:
    pre_text = pdf.pages[0].extract_text() or ""
check("synthetic scan genuinely has no text layer before OCR", pre_text.strip() == "")

fid_scan = upload(token, "scan.pdf", scanned_bytes).json()["file_id"]
r = requests.post(f"{BASE}/ocr", headers=auth_header(token), json={"file_id": fid_scan})
result = wait_job(token, r.json()["job_id"], timeout=45)
check("OCR job succeeds", result["status"] == "done", str(result))
ocr_result = result.get("result") or {}
check("OCR marks the page as OCR'd", ocr_result.get("pages_ocred") == [1], str(ocr_result))
word_boxes = ocr_result.get("word_boxes", {}).get("1", [])
check("OCR returns word boxes", len(word_boxes) > 0)
check("OCR script detection ran and found Latin (with reasonable confidence)",
      (ocr_result.get("script_detection") or {}).get("script") == "Latin" and
      (ocr_result.get("script_detection") or {}).get("script_confidence", 0) > 5,
      str(ocr_result.get("script_detection")))

ocred_pdf_id = ocr_result.get("file_id")
if ocred_pdf_id:
    content = requests.get(f"{BASE}/files/{ocred_pdf_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        post_text = pdf.pages[0].extract_text() or ""
    check("OCR made the scan genuinely searchable", "OCRMARKER8899" in post_text, repr(post_text))

# selective OCR: hybrid doc, only the scanned page should be touched
from pypdf import PdfReader as _PR, PdfWriter as _PW
real_text_pdf = make_pdf_bytes(["This page already has real text."])
scan_page_pdf = make_scanned_pdf_bytes("HYBRIDTEST42 second page")
w = _PW()
for src in (real_text_pdf, scan_page_pdf):
    for p in _PR(io.BytesIO(src)).pages:
        w.add_page(p)
hybrid_buf = io.BytesIO()
w.write(hybrid_buf)
fid_hybrid = upload(token, "hybrid.pdf", hybrid_buf.getvalue()).json()["file_id"]
r = requests.post(f"{BASE}/ocr", headers=auth_header(token), json={"file_id": fid_hybrid})
result = wait_job(token, r.json()["job_id"], timeout=45)
hybrid_out = result.get("result") or {}
check("selective OCR only touches the scanned page", hybrid_out.get("pages_ocred") == [2],
      str(hybrid_out))
if hybrid_out.get("file_id"):
    content = requests.get(f"{BASE}/files/{hybrid_out['file_id']}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        p1_text = pdf.pages[0].extract_text() or ""
        p2_text = pdf.pages[1].extract_text() or ""
    check("hybrid page 1 (real text) preserved untouched",
          "already has real text" in p1_text, repr(p1_text))
    check("hybrid page 2 (scanned) now searchable", "HYBRIDTEST42" in p2_text, repr(p2_text))

# force flag
r = requests.post(f"{BASE}/ocr", headers=auth_header(token),
                   json={"file_id": fid_hybrid, "force": True})
result = wait_job(token, r.json()["job_id"], timeout=45)
force_out = result.get("result") or {}
check("force=True re-OCRs every page including ones with real text",
      force_out.get("pages_ocred") == [1, 2], str(force_out))


# ============================================================
print("=" * 60)
print("PHASE 5 — Security, redaction, watermark, forms")
print("=" * 60)

fid_secret = upload(token, "secret.pdf", make_pdf_bytes(["SECRETVALUE3344"])).json()["file_id"]

sec_before = requests.get(f"{BASE}/pdf/{fid_secret}/security", headers=auth_header(token))
check("unencrypted file reports encrypted=false", sec_before.json().get("encrypted") is False)

r = requests.post(f"{BASE}/pdf/encrypt", headers=auth_header(token),
                   json={"file_id": fid_secret, "user_password": "pw12345",
                         "allow_printing": False, "allow_copying": False})
result = wait_job(token, r.json()["job_id"])
enc_id = result.get("result")
check("encrypt job succeeds", result["status"] == "done", str(result))

sec_nopass = requests.get(f"{BASE}/pdf/{enc_id}/security", headers=auth_header(token))
check("encrypted file reports requires_password=true with no password",
      sec_nopass.json().get("requires_password") is True, sec_nopass.text)

sec_pass = requests.post(f"{BASE}/pdf/{enc_id}/security", headers=auth_header(token), json={"password": "pw12345"})
perms = sec_pass.json().get("permissions") or {}
check("correct password reveals restricted permissions",
      perms.get("extract") is False and perms.get("print_lowres") is False, str(perms))

enc_bytes = requests.get(f"{BASE}/files/{enc_id}", headers=auth_header(token)).content
opened_without_pw = True
try:
    pikepdf.open(io.BytesIO(enc_bytes))
except pikepdf.PasswordError:
    opened_without_pw = False
check("encrypted file genuinely cannot be opened without a password", not opened_without_pw)

with pikepdf.open(io.BytesIO(enc_bytes), password="pw12345") as pdf:
    pdf.save(io.BytesIO())  # just confirm it opens without raising
check("encrypted file opens fine with correct password", True)

wrong_dec = requests.post(f"{BASE}/pdf/decrypt", headers=auth_header(token),
                           json={"file_id": enc_id, "password": "wrongpw"})
wrong_result = wait_job(token, wrong_dec.json()["job_id"])
check("decrypt with wrong password fails", wrong_result["status"] == "failed", str(wrong_result))

right_dec = requests.post(f"{BASE}/pdf/decrypt", headers=auth_header(token),
                           json={"file_id": enc_id, "password": "pw12345"})
right_result = wait_job(token, right_dec.json()["job_id"])
dec_id = right_result.get("result")
check("decrypt with correct password succeeds", right_result["status"] == "done", str(right_result))
if dec_id:
    sec_after = requests.get(f"{BASE}/pdf/{dec_id}/security", headers=auth_header(token))
    check("decrypted file reports encrypted=false", sec_after.json().get("encrypted") is False)
    content = requests.get(f"{BASE}/files/{dec_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    check("decrypted content intact", "SECRETVALUE3344" in text, repr(text))

# redaction
fid_redact_src = upload(token, "redact_src.pdf",
                         make_pdf_bytes(["Public line here", "SECRETVALUE3344 sensitive"])).json()["file_id"]
r = requests.post(f"{BASE}/pdf/redact", headers=auth_header(token),
                   json={"file_id": fid_redact_src,
                         "regions": [{"page": 1, "x": 70, "y": 655, "width": 300, "height": 20}]})
result = wait_job(token, r.json()["job_id"], timeout=45)
check("redact job succeeds", result["status"] == "done", str(result))
red_out = result.get("result") or {}
check("redact reports correct page redacted", red_out.get("pages_redacted") == [1], str(red_out))
redacted_id = red_out.get("file_id")
if redacted_id:
    content = requests.get(f"{BASE}/files/{redacted_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        red_text = pdf.pages[0].extract_text() or ""
    check("redacted page has zero extractable text (true removal, not visual-only)",
          red_text.strip() == "", repr(red_text))

# redact a page in a multi-page doc; unredacted page should stay untouched
multi_src = make_blank_pdf_bytes(1)
from pypdf import PdfWriter as _PW2, PdfReader as _PR2
combined = _PW2()
for p in _PR2(io.BytesIO(make_pdf_bytes(["Page one untouched CONTROL111"]))).pages:
    combined.add_page(p)
for p in _PR2(io.BytesIO(make_pdf_bytes(["Page two SENSITIVE222 has secret"]))).pages:
    combined.add_page(p)
buf2 = io.BytesIO()
combined.write(buf2)
fid_multi = upload(token, "multi.pdf", buf2.getvalue()).json()["file_id"]
r = requests.post(f"{BASE}/pdf/redact", headers=auth_header(token),
                   json={"file_id": fid_multi, "regions": [{"page": 2, "x": 70, "y": 655, "width": 300, "height": 40}]})
result = wait_job(token, r.json()["job_id"], timeout=45)
multi_out = result.get("result") or {}
check("redact on page 2 only reports page 2", multi_out.get("pages_redacted") == [2], str(multi_out))
if multi_out.get("file_id"):
    content = requests.get(f"{BASE}/files/{multi_out['file_id']}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        p1t = pdf.pages[0].extract_text() or ""
        p2t = pdf.pages[1].extract_text() or ""
    check("unredacted page 1 text fully preserved", "CONTROL111" in p1t, repr(p1t))
    check("redacted page 2 has no extractable text", p2t.strip() == "", repr(p2t))

# watermark
fid_wm_src = upload(token, "wm_src.pdf", make_pdf_bytes(["Watermark source content ABCDEF"])).json()["file_id"]
r = requests.post(f"{BASE}/pdf/watermark", headers=auth_header(token),
                   json={"file_id": fid_wm_src, "text": "SAMPLE WATERMARK", "opacity": 0.3})
result = wait_job(token, r.json()["job_id"])
wm_id = result.get("result")
check("watermark job succeeds", result["status"] == "done", str(result))
if wm_id:
    content = requests.get(f"{BASE}/files/{wm_id}", headers=auth_header(token)).content
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        chars = "".join(c["text"] for c in pdf.pages[0].chars)
    check("watermark text present alongside original content",
          all(ch in chars for ch in "SAMPLE") and "ABCDEF" in chars)

# forms
fid_form_src = upload(token, "form_src.pdf", make_pdf_bytes(["Form test"])).json()["file_id"]
r = requests.post(f"{BASE}/forms/create", headers=auth_header(token),
                   json={"file_id": fid_form_src, "fields": [
                       {"name": "full_name", "type": "text", "page": 1, "x": 150, "y": 700, "width": 200, "height": 16},
                       {"name": "agree", "type": "checkbox", "page": 1, "x": 150, "y": 670, "width": 14, "height": 14},
                   ]})
result = wait_job(token, r.json()["job_id"])
form_id = result.get("result")
check("form creation succeeds", result["status"] == "done", str(result))

if form_id:
    data_before = requests.get(f"{BASE}/forms/{form_id}/data", headers=auth_header(token)).json()
    check("form fields exist before filling",
          set(data_before.get("fields", {}).keys()) == {"full_name", "agree"}, str(data_before))
    check("checkbox defaults to /Off before filling",
          data_before.get("fields", {}).get("agree") == "/Off", str(data_before))

    r = requests.post(f"{BASE}/forms/fill", headers=auth_header(token),
                       json={"file_id": form_id, "data": {"full_name": "Test Person", "agree": "/Yes"}})
    result = wait_job(token, r.json()["job_id"])
    filled_id = result.get("result")
    check("form fill succeeds", result["status"] == "done", str(result))

    if filled_id:
        data_after = requests.get(f"{BASE}/forms/{filled_id}/data", headers=auth_header(token)).json()
        check("filled form shows correct text value",
              data_after.get("fields", {}).get("full_name") == "Test Person", str(data_after))
        check("filled form shows correct checkbox value",
              data_after.get("fields", {}).get("agree") == "/Yes", str(data_after))

# invalid field type rejected
bad_field = requests.post(f"{BASE}/forms/create", headers=auth_header(token),
                           json={"file_id": fid_form_src, "fields": [
                               {"name": "x", "type": "radio", "page": 1, "x": 0, "y": 0, "width": 10, "height": 10}
                           ]})
bad_result = wait_job(token, bad_field.json()["job_id"]) if bad_field.status_code == 200 else {"status": "failed"}
check("invalid form field type rejected", bad_result["status"] == "failed", str(bad_result))


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
