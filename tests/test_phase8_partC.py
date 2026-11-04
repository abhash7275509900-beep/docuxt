import os
"""
Regression test — Phase 8 Part C (table extraction).
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


def make_table_pdf_bytes(data):
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(612, 792))
    t = Table(data)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ]))
    doc.build([t])
    return buf.getvalue()


def make_text_pdf_bytes(text):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.drawString(72, 700, text)
    c.save()
    return buf.getvalue()


signup = requests.post(f"{BASE}/auth/signup",
                        json={"email": "phase8c_test@test.com", "password": "testpass12"})
token = signup.json()["token"] if signup.status_code == 200 else \
    requests.post(f"{BASE}/auth/login", json={"email": "phase8c_test@test.com", "password": "testpass12"}).json()["token"]
check("test user authenticated", bool(token))


# ============================================================
print("=" * 60)
print("Table extraction")
print("=" * 60)

table_data = [
    ["Product", "Q1 Sales", "Q2 Sales"],
    ["Widget A", "1200", "1450"],
    ["Widget B", "890", "920"],
    ["Widget C", "450", "610"],
]
fid_table = upload(token, "table.pdf", make_table_pdf_bytes(table_data)).json()["file_id"]

r = requests.post(f"{BASE}/pdf/extract-tables", headers=auth_header(token),
                   json={"file_id": fid_table, "format": "xlsx"})
result = wait_job(token, r.json()["job_id"])
check("extract-tables xlsx job succeeds", result["status"] == "done", str(result))
out = result.get("result") or {}
check("reports exactly 1 table found", out.get("tables_found") == 1, str(out))

xlsx_content = requests.get(f"{BASE}/files/{out.get('file_id')}", headers=auth_header(token)).content
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(xlsx_content))
rows = list(wb[wb.sheetnames[0]].iter_rows(values_only=True))
expected_rows = [tuple(r) for r in table_data]
check("xlsx output has exact correct data, row for row",
      rows == expected_rows, f"got {rows}")

r2 = requests.post(f"{BASE}/pdf/extract-tables", headers=auth_header(token),
                    json={"file_id": fid_table, "format": "csv"})
result2 = wait_job(token, r2.json()["job_id"])
check("extract-tables csv job succeeds", result2["status"] == "done", str(result2))
csv_out = result2.get("result") or {}
csv_content = requests.get(f"{BASE}/files/{csv_out.get('file_id')}", headers=auth_header(token)).content.decode()
check("csv output contains all the real data values",
      all(cell in csv_content for row in table_data for cell in row), repr(csv_content))

# no-tables edge case
fid_notable = upload(token, "notable.pdf", make_text_pdf_bytes("Just plain text, no tables.")).json()["file_id"]
r3 = requests.post(f"{BASE}/pdf/extract-tables", headers=auth_header(token),
                    json={"file_id": fid_notable, "format": "xlsx"})
result3 = wait_job(token, r3.json()["job_id"])
check("no-tables case doesn't crash, reports 0 found",
      result3["status"] == "done" and (result3.get("result") or {}).get("tables_found") == 0,
      str(result3))
no_table_xlsx = requests.get(f"{BASE}/files/{result3['result']['file_id']}", headers=auth_header(token)).content
try:
    load_workbook(io.BytesIO(no_table_xlsx))
    valid_empty_xlsx = True
except Exception:
    valid_empty_xlsx = False
check("the zero-tables xlsx output is still a genuinely valid, non-corrupted file", valid_empty_xlsx)

bad_format = requests.post(f"{BASE}/pdf/extract-tables", headers=auth_header(token),
                            json={"file_id": fid_table, "format": "pdf"})
bad_result = wait_job(token, bad_format.json()["job_id"])
check("invalid format value rejected", bad_result["status"] == "failed", str(bad_result))


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
