"""
DocEngine API server.

Implements Phases 0 through 8 with full input validation, security headers,
collaborator/lock enforcement, and static frontend serving.
"""
import inspect
import io
import json
import math
import os
import re
import sqlite3
import time
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from werkzeug.exceptions import HTTPException

from app import storage, jobs, db, auth
from app.operations import (
    parse_bool,
    parse_dpi,
    get_displayed_page_size,
    pages as page_ops,
    compress as compress_ops,
    convert_images as image_ops,
    annotations as ann_ops,
    office_convert,
    extract as extract_ops,
    validate as validate_ops,
    ocr as ocr_ops,
    security as security_ops,
    redact as redact_ops,
    watermark as watermark_ops,
    forms as forms_ops,
    ai_text as ai_ops,
    metadata as metadata_ops,
    structure as structure_ops,
    tables as tables_ops,
    compare as compare_ops,
    duplicates as duplicates_ops,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB cap

db.init_db()

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_MASK_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_SAFE_INLINE_FORMATS = {"pdf", "image", "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "txt", "csv", "md"}
_TEXT_INLINE_EXTS = {".txt", ".csv", ".md"}


class ConflictError(Exception):
    pass


def _add_security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; "
        "style-src 'self' 'unsafe-inline'; object-src 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


if hasattr(app, "after_request"):
    app.after_request(_add_security_headers)


# ---------- error handling ----------

@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    if isinstance(e, FileNotFoundError):
        return jsonify(error=str(e)), 404
    if isinstance(e, ConflictError):
        return jsonify(error=str(e)), 409
    if isinstance(e, KeyError):
        return jsonify(error=f"missing required field: {e.args[0] if e.args else ''}"), 400
    if isinstance(e, (ValueError, TypeError)):
        return jsonify(error=str(e)), 400
    if isinstance(e, PermissionError):
        return jsonify(error=str(e)), 403
    app.logger.exception(e)
    return jsonify(error="internal_error"), 500


def _body(optional: bool = False):
    data = request.get_json(force=True, silent=optional)
    if data is None and optional:
        return {}
    if not isinstance(data, dict):
        raise ValueError("JSON object body required")
    return data


def _require_str_id(val, name: str = "file_id") -> str:
    if not isinstance(val, str) or not val.strip() or len(val) > 256:
        raise ValueError(f"{name} must be a non-empty string")
    return val


def _validate_email(email: str) -> str:
    if not isinstance(email, str):
        raise ValueError("valid email required")
    email = email.strip().lower()
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("valid email required")
    return email


def _require_nonempty_list(val, name="file_ids"):
    if not isinstance(val, list) or len(val) == 0:
        raise ValueError(f"{name} must be a non-empty list")
    return val


def _check_owns(file_id):
    _require_str_id(file_id, "file_id")
    meta = db.get_file(file_id)
    if meta is None:
        raise FileNotFoundError("no such file_id")
    if meta["owner_id"] != g.user_id:
        raise PermissionError("you do not have access to this file")
    return meta


def _check_file_permission(file_id, required="view"):
    _require_str_id(file_id, "file_id")
    meta = db.get_file(file_id)
    if meta is None:
        raise FileNotFoundError("no such file_id")
    perm = db.get_file_permission(g.user_id, file_id)
    ranks = {"view": 0, "comment": 1, "edit": 2, "owner": 3}
    if perm is None or ranks.get(perm, -1) < ranks[required]:
        raise PermissionError(f"you do not have '{required}' access to this file")
    return meta


def _check_owns_all(file_ids):
    _require_nonempty_list(file_ids, "file_ids")
    for fid in file_ids:
        _check_owns(fid)


_PERMISSION_RANK = {"view": 0, "comment": 1, "edit": 2}


def _check_document_access(doc_id, required="view"):
    _require_str_id(doc_id, "doc_id")
    doc = db.get_document(doc_id)
    if doc is None:
        raise FileNotFoundError("no such document")
    if doc["owner_id"] == g.user_id:
        return doc
    perm = db.get_collaborator_permission(doc_id, g.user_id)
    if perm is None or _PERMISSION_RANK[perm] < _PERMISSION_RANK[required]:
        raise PermissionError(f"you do not have '{required}' access to this document")
    return doc


def _check_owns_document(doc_id):
    _require_str_id(doc_id, "doc_id")
    doc = db.get_document(doc_id)
    if doc is None:
        raise FileNotFoundError("no such document")
    if doc["owner_id"] != g.user_id:
        raise PermissionError("you do not have access to this document")
    return doc


def _check_document_not_locked_by_other(doc_id):
    if db.is_document_locked_by_other(doc_id, g.user_id):
        raise ConflictError("document is currently locked by another user")


def _resolve_share(token, required_permissions=None):
    _require_str_id(token, "token")
    link = db.get_share_link(token)
    if link is None:
        raise FileNotFoundError("no such share link")
    if link["revoked"]:
        raise PermissionError("this share link has been revoked")
    if link["expires_at"] is not None and time.time() > link["expires_at"]:
        raise PermissionError("this share link has expired")
    if required_permissions and link["permission"] not in required_permissions:
        raise PermissionError(
            f"this share link only allows '{link['permission']}', not {required_permissions}"
        )
    return link


# ---------- frontend serving ----------

@app.get("/")
def serve_frontend_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(index_path):
        return send_file(index_path, mimetype="text/html")
    return jsonify(status="ok", service="DocEngine API")


@app.get("/frontend/<path:filename>")
def serve_frontend_asset(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ---------- auth ----------

@app.post("/auth/signup")
def signup():
    body = _body()
    email = _validate_email(body.get("email", ""))
    password = auth.validate_password(body.get("password"))
    if db.get_user_by_email(email):
        return jsonify(error="email already registered"), 409

    try:
        user_id = db.create_user(email, auth.hash_password(password))
    except sqlite3.IntegrityError:
        return jsonify(error="email already registered"), 409
    token = auth.issue_token(user_id)
    return jsonify(user_id=user_id, token=token)


@app.post("/auth/login")
def login():
    body = _body()
    raw_email = body.get("email")
    email = raw_email.strip().lower() if isinstance(raw_email, str) else ""
    raw_pw = body.get("password")
    if not isinstance(raw_pw, str) or len(raw_pw) > auth.MAX_PASSWORD_LEN:
        raise ValueError(f"password must be a string of at most {auth.MAX_PASSWORD_LEN} characters")
    password = raw_pw
    user = db.get_user_by_email(email) if email else None
    if not user:
        auth.verify_dummy_password(password)
        return jsonify(error="invalid email or password"), 401
    if not auth.verify_password(password, user["password_hash"]):
        return jsonify(error="invalid email or password"), 401
    token = auth.issue_token(user["id"])
    return jsonify(user_id=user["id"], token=token)


@app.get("/auth/me")
@auth.require_auth
def me():
    return jsonify(user_id=g.user_id, email=g.user["email"])


# ---------- file management ----------

@app.post("/files")
@auth.require_auth
def upload_file():
    if "file" not in request.files:
        return jsonify(error="no file part named 'file'"), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="empty filename"), 400
    file_id = storage.save_upload_stream(f, f.filename)

    fmt, val_error = validate_ops.validate(storage.resolve(file_id), f.filename)
    if val_error:
        storage.delete_file(file_id)
        return jsonify(error="validation_failed", detail=val_error), 400

    size = storage.size_of(file_id)
    db.register_file(file_id, g.user_id, original_name=os.path.basename(f.filename),
                      kind="upload", size=size, format=fmt)
    return jsonify(file_id=file_id, size=size, format=fmt)


@app.get("/files")
@auth.require_auth
def list_files():
    return jsonify(files=db.list_files_for_user(g.user_id))


@app.delete("/files/<file_id>")
@auth.require_auth
def delete_file_endpoint(file_id):
    _check_owns(file_id)
    if db.file_used_in_document(file_id):
        raise ConflictError("file is used by a document version; delete the document first or keep the file")
    storage.delete_file(file_id)
    db.delete_file(file_id, g.user_id)
    return jsonify(deleted=True, file_id=file_id)


@app.get("/files/<file_id>")
@auth.require_auth
def download_file(file_id):
    meta = _check_file_permission(file_id, required="view")
    path = storage.resolve(file_id)
    want_inline = request.args.get("inline") in ("1", "true", "yes")
    ext = os.path.splitext(file_id)[1].lower()
    fmt = (meta.get("format") or "").lower()
    safe_inline = want_inline and (fmt in _SAFE_INLINE_FORMATS or ext in _TEXT_INLINE_EXTS) and ext != ".html"
    mimetype = "text/plain; charset=utf-8" if (safe_inline and ext in _TEXT_INLINE_EXTS) else None
    dl_name = os.path.basename(meta.get("original_name") or file_id)
    return send_file(path, mimetype=mimetype, as_attachment=not safe_inline, download_name=dl_name)


@app.get("/files/<file_id>/info")
@auth.require_auth
def file_info(file_id):
    meta = _check_file_permission(file_id, required="view")
    info = {"file_id": file_id, "size": storage.size_of(file_id), "meta": meta}
    if file_id.lower().endswith(".pdf") or (meta and meta.get("format") == "pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(storage.resolve(file_id))
            is_enc = bool(reader.is_encrypted)
            info["encrypted"] = is_enc
            readable = not is_enc
            if is_enc:
                try:
                    if reader.decrypt("") != 0:
                        readable = True
                        info["requires_password"] = False
                    else:
                        info["requires_password"] = True
                except Exception:
                    info["requires_password"] = True
            else:
                info["requires_password"] = False

            if readable:
                info["pages"] = len(reader.pages)
                if len(reader.pages) > 0:
                    w0, h0, rot0 = get_displayed_page_size(reader.pages[0])
                    info["page_width"] = round(w0, 2)
                    info["page_height"] = round(h0, 2)
                    info["rotation"] = rot0
                    info["page_sizes"] = [
                        {
                            "page": idx + 1,
                            "width": round(pw, 2),
                            "height": round(ph, 2),
                            "rotation": prot,
                        }
                        for idx, (pw, ph, prot) in enumerate(
                            get_displayed_page_size(p) for p in reader.pages
                        )
                    ]
        except Exception:
            info["encrypted"] = True
            info["requires_password"] = True
    return jsonify(info)


@app.get("/files/<file_id>/preview")
@auth.require_auth
def preview_pdf_page(file_id):
    """Renders a 1-indexed PDF page to PNG for interactive frontend preview/canvas."""
    meta = _check_file_permission(file_id, required="view")
    if not (file_id.lower().endswith(".pdf") or (meta and meta.get("format") == "pdf")):
        raise ValueError("preview is only supported for PDF files")
    try:
        page_num = int(request.args.get("page", 1))
    except (ValueError, TypeError) as e:
        raise ValueError("page must be an integer") from e
    if page_num < 1:
        raise ValueError("page must be >= 1")
    try:
        raw_dpi = int(request.args.get("dpi", 120))
    except (ValueError, TypeError) as e:
        raise ValueError("dpi must be an integer") from e
    dpi = parse_dpi(raw_dpi, default=120, min_dpi=36, max_dpi=600)

    path = storage.resolve(file_id)
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as e:
        raise ValueError("PDF is password-protected or cannot be rendered for preview") from e

    try:
        total = len(pdf)
        if page_num > total:
            raise ValueError(f"page {page_num} out of range (1..{total})")
        page = pdf[page_num - 1]
        pw, ph = page.get_size()
        if (pw * dpi / 72.0) * (ph * dpi / 72.0) > 40_000_000:
            raise ValueError("page is too large to preview at this dpi; lower dpi")
        bitmap = page.render(scale=dpi / 72.0)
        pil_img = bitmap.to_pil()
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png", as_attachment=False, download_name=f"page_{page_num}.png")
    finally:
        if hasattr(pdf, "close"):
            try:
                pdf.close()
            except Exception:
                pass


# ---------- pdf page operations ----------

@app.post("/pdf/merge")
@auth.require_auth
def merge():
    body = _body()
    file_ids = _require_nonempty_list(body.get("file_ids"), "file_ids")
    _check_owns_all(file_ids)
    job_id = jobs.submit(page_ops.merge, owner_id=g.user_id,
                          source_file_id=file_ids[0], file_ids=file_ids)
    return jsonify(job_id=job_id)


@app.post("/pdf/split")
@auth.require_auth
def split():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(page_ops.split, owner_id=g.user_id,
                          source_file_id=body["file_id"],
                          file_id=body["file_id"], ranges=body.get("ranges"))
    return jsonify(job_id=job_id)


@app.post("/pdf/reorder")
@auth.require_auth
def reorder():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(page_ops.reorder, owner_id=g.user_id,
                          source_file_id=body["file_id"],
                          file_id=body["file_id"], order=body["order"])
    return jsonify(job_id=job_id)


@app.post("/pdf/rotate")
@auth.require_auth
def rotate():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(
        page_ops.rotate, owner_id=g.user_id, source_file_id=body["file_id"],
        file_id=body["file_id"], pages=body.get("pages"),
        degrees=body.get("degrees", 90),
    )
    return jsonify(job_id=job_id)


@app.post("/pdf/delete-pages")
@auth.require_auth
def delete_pages():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(page_ops.delete_pages, owner_id=g.user_id,
                          source_file_id=body["file_id"],
                          file_id=body["file_id"], pages=body["pages"])
    return jsonify(job_id=job_id)


@app.post("/pdf/extract-pages")
@auth.require_auth
def extract_pages():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(page_ops.extract_pages, owner_id=g.user_id,
                          source_file_id=body["file_id"],
                          file_id=body["file_id"], pages=body["pages"])
    return jsonify(job_id=job_id)


@app.post("/pdf/compress")
@auth.require_auth
def compress():
    body = _body()
    _check_owns(body["file_id"])
    job_id = jobs.submit(
        compress_ops.compress, owner_id=g.user_id, source_file_id=body["file_id"],
        file_id=body["file_id"], quality=body.get("quality", "medium"),
    )
    return jsonify(job_id=job_id)


# ---------- conversion ----------

@app.post("/convert/images-to-pdf")
@auth.require_auth
def images_to_pdf():
    body = _body()
    file_ids = _require_nonempty_list(body.get("file_ids"), "file_ids")
    _check_owns_all(file_ids)
    job_id = jobs.submit(image_ops.images_to_pdf, owner_id=g.user_id,
                          source_file_id=file_ids[0], file_ids=file_ids)
    return jsonify(job_id=job_id)


@app.post("/convert/pdf-to-images")
@auth.require_auth
def pdf_to_images():
    body = _body()
    _check_owns(body["file_id"])
    dpi = parse_dpi(body.get("dpi", 150), default=150, min_dpi=36, max_dpi=600)
    job_id = jobs.submit(
        image_ops.pdf_to_images, owner_id=g.user_id, source_file_id=body["file_id"],
        file_id=body["file_id"], fmt=body.get("fmt", "png"), dpi=dpi,
    )
    return jsonify(job_id=job_id)


# ---------- office format conversion (Phase 3) ----------

@app.post("/convert/office")
@auth.require_auth
def convert_office():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    src_ext = os.path.splitext(file_id)[1].lower()
    office_convert.validate_conversion_pair(src_ext, body.get("target_format"))
    job_id = jobs.submit(
        office_convert.convert, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, target_format=body["target_format"],
    )
    return jsonify(job_id=job_id)


@app.get("/convert/office/formats")
@auth.require_auth
def office_formats():
    res = {
        "formats": sorted(office_convert.FORMAT_FILTERS.keys()),
        "compatibility": office_convert.COMPATIBLE_TARGETS,
    }
    src = request.args.get("source")
    if src:
        ext = "." + src.lower().lstrip(".")
        res["allowed_targets"] = office_convert.COMPATIBLE_TARGETS.get(ext, [])
    return jsonify(res)


# ---------- lightweight extraction (Phase 3, Part B) ----------

@app.post("/extract/text")
@auth.require_auth
def extract_text():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    fmt = body.get("format", "text")
    fn = {
        "text": extract_ops.pdf_to_text,
        "html": extract_ops.pdf_to_html,
        "markdown": extract_ops.pdf_to_markdown,
    }.get(fmt)
    if fn is None:
        return jsonify(error="format must be one of text, html, markdown"), 400
    job_id = jobs.submit(fn, owner_id=g.user_id, source_file_id=file_id, file_id=file_id)
    return jsonify(job_id=job_id)


# ---------- OCR (Phase 4) ----------

@app.post("/ocr")
@auth.require_auth
def run_ocr():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    dpi = parse_dpi(body.get("dpi", ocr_ops.DEFAULT_DPI), default=ocr_ops.DEFAULT_DPI, min_dpi=36, max_dpi=600)
    force = parse_bool(body.get("force", False), "force")
    job_id = jobs.submit(
        ocr_ops.ocr, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, lang=body.get("lang", "eng"),
        dpi=dpi, force=force,
    )
    return jsonify(job_id=job_id)


# ---------- security (Phase 5) ----------

@app.post("/pdf/encrypt")
@auth.require_auth
def encrypt_pdf():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    allow_printing = parse_bool(body.get("allow_printing", True), "allow_printing")
    allow_copying = parse_bool(body.get("allow_copying", True), "allow_copying")
    allow_modification = parse_bool(body.get("allow_modification", False), "allow_modification")
    job_id = jobs.submit(
        security_ops.encrypt, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id,
        user_password=body.get("user_password", ""),
        owner_password=body.get("owner_password"),
        allow_printing=allow_printing,
        allow_copying=allow_copying,
        allow_modification=allow_modification,
    )
    return jsonify(job_id=job_id)


@app.post("/pdf/decrypt")
@auth.require_auth
def decrypt_pdf():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        security_ops.decrypt, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, password=body.get("password", ""),
    )
    return jsonify(job_id=job_id)


@app.route("/pdf/<file_id>/security", methods=["GET", "POST"])
@auth.require_auth
def inspect_security(file_id):
    _check_owns(file_id)
    if "password" in request.args:
        raise ValueError("do not pass password in query string; use POST JSON body or X-PDF-Password header")
    password = request.headers.get("X-PDF-Password", "")
    if request.method == "POST":
        body = _body(optional=True)
        if "password" in body and body["password"] is not None:
            password = body["password"]
    return jsonify(security_ops.inspect(file_id, password=password))


# ---------- redaction (Phase 5) ----------

@app.post("/pdf/redact")
@auth.require_auth
def redact_pdf():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        redact_ops.redact, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, regions=body["regions"],
    )
    return jsonify(job_id=job_id)


# ---------- watermark (Phase 5) ----------

@app.post("/pdf/watermark")
@auth.require_auth
def watermark_pdf():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        watermark_ops.add_watermark, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, text=body["text"],
        opacity=body.get("opacity", 0.3), font_size=body.get("font_size", 40),
        color=body.get("color", "#888888"), diagonal=body.get("diagonal", True),
        pages=body.get("pages"),
    )
    return jsonify(job_id=job_id)


# ---------- forms (Phase 5) ----------

@app.post("/forms/create")
@auth.require_auth
def create_form():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        forms_ops.create_form, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, fields=body["fields"],
    )
    return jsonify(job_id=job_id)


@app.post("/forms/fill")
@auth.require_auth
def fill_form():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        forms_ops.fill_form, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, data=body["data"],
    )
    return jsonify(job_id=job_id)


@app.get("/forms/<file_id>/data")
@auth.require_auth
def get_form_data(file_id):
    _check_owns(file_id)
    return jsonify(fields=forms_ops.extract_form_data(file_id))


# ---------- AI-assisted (Phase 7) ----------

@app.post("/ai/summarize")
@auth.require_auth
def summarize_document():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        ai_ops.summarize, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, num_sentences=body.get("sentences", 3),
    )
    return jsonify(job_id=job_id)


@app.post("/ai/ask")
@auth.require_auth
def ask_document():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        ai_ops.ask, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, question=body["question"], top_k=body.get("top_k", 3),
    )
    return jsonify(job_id=job_id)


@app.post("/ai/detect-pii")
@auth.require_auth
def detect_pii():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        ai_ops.detect_pii, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id,
    )
    return jsonify(job_id=job_id)


# ---------- metadata (Phase 8) ----------

@app.get("/pdf/<file_id>/metadata")
@auth.require_auth
def get_metadata(file_id):
    _check_owns(file_id)
    return jsonify(metadata_ops.get_metadata(file_id))


@app.post("/pdf/metadata")
@auth.require_auth
def set_metadata():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    fields = body.get("fields")
    custom = body.get("custom")
    if fields is not None and not isinstance(fields, dict):
        raise ValueError("fields must be a JSON object")
    if custom is not None and not isinstance(custom, dict):
        raise ValueError("custom must be a JSON object")
    job_id = jobs.submit(
        metadata_ops.set_metadata, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, fields=fields, custom=custom,
    )
    return jsonify(job_id=job_id)


# ---------- bookmarks & hyperlinks (Phase 8) ----------

@app.post("/pdf/bookmarks")
@auth.require_auth
def add_bookmark():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        structure_ops.add_bookmark, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, title=body["title"], page_number=body["page"],
        parent_title=body.get("parent_title"),
    )
    return jsonify(job_id=job_id)


@app.get("/pdf/<file_id>/bookmarks")
@auth.require_auth
def list_bookmarks(file_id):
    _check_owns(file_id)
    return jsonify(bookmarks=structure_ops.list_bookmarks(file_id))


@app.post("/pdf/hyperlinks")
@auth.require_auth
def add_hyperlink():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        structure_ops.add_hyperlink, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, page_number=body["page"], x=body["x"], y=body["y"],
        width=body["width"], height=body["height"],
        url=body.get("url"), target_page=body.get("target_page"),
    )
    return jsonify(job_id=job_id)


@app.get("/pdf/<file_id>/hyperlinks")
@auth.require_auth
def list_hyperlinks(file_id):
    _check_owns(file_id)
    return jsonify(hyperlinks=structure_ops.list_hyperlinks(file_id))


# ---------- table extraction (Phase 8) ----------

@app.post("/pdf/extract-tables")
@auth.require_auth
def extract_tables():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        tables_ops.extract_tables, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, fmt=body.get("format", "xlsx"), pages=body.get("pages"),
    )
    return jsonify(job_id=job_id)


# ---------- comparison & analysis (Phase 8) ----------

@app.post("/pdf/compare")
@auth.require_auth
def compare_documents():
    body = _body()
    fid_a, fid_b = body["file_id_a"], body["file_id_b"]
    _check_owns_all([fid_a, fid_b])
    job_id = jobs.submit(
        compare_ops.compare, owner_id=g.user_id, source_file_id=fid_a,
        file_id_a=fid_a, file_id_b=fid_b,
    )
    return jsonify(job_id=job_id)


@app.get("/pdf/<file_id>/analyze")
@auth.require_auth
def analyze_document(file_id):
    _check_owns(file_id)
    return jsonify(compare_ops.analyze(file_id))


@app.post("/pdf/find-duplicate-pages")
@auth.require_auth
def find_duplicate_pages():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    job_id = jobs.submit(
        duplicates_ops.find_duplicate_pages, owner_id=g.user_id, source_file_id=file_id,
        file_id=file_id, threshold=body.get("threshold"),
        text_threshold=body.get("text_threshold"),
        image_distance=body.get("image_distance"),
    )
    return jsonify(job_id=job_id)


# ---------- batch operations (Phase 8) ----------

_BATCH_OPERATIONS = {
    "compress": compress_ops.compress,
    "watermark": watermark_ops.add_watermark,
    "rotate": page_ops.rotate,
    "ocr": ocr_ops.ocr,
    "encrypt": security_ops.encrypt,
}


@app.post("/batch")
@auth.require_auth
def batch_operation():
    body = _body()
    operation = body["operation"]
    fn = _BATCH_OPERATIONS.get(operation)
    if fn is None:
        return jsonify(error=f"operation must be one of {sorted(_BATCH_OPERATIONS)}"), 400
    file_ids = _require_nonempty_list(body.get("file_ids"), "file_ids")
    _check_owns_all(file_ids)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")

    allowed_params = [k for k in inspect.signature(fn).parameters.keys() if k != "file_id"]
    unknown_params = [k for k in params.keys() if k not in allowed_params]
    if unknown_params:
        raise ValueError(
            f"unknown parameter(s) for {operation}: {unknown_params}. Allowed: {allowed_params}"
        )

    file_ids = list(dict.fromkeys(file_ids))
    job_ids = {}
    for file_id in file_ids:
        job_ids[file_id] = jobs.submit(
            fn, owner_id=g.user_id, source_file_id=file_id, file_id=file_id, **params
        )
    return jsonify(job_ids=job_ids)


# ---------- annotations (Phase 2) ----------

@app.post("/annotations")
@auth.require_auth
def create_annotation():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    ann_id = ann_ops.add_annotation(
        file_id, g.user_id, body["type"], body["page"], body.get("data", {})
    )
    return jsonify(annotation_id=ann_id)


@app.get("/annotations/<file_id>")
@auth.require_auth
def get_annotations(file_id):
    _check_owns(file_id)
    return jsonify(annotations=ann_ops.list_annotations(file_id))


@app.route("/annotations/<annotation_id>", methods=["PATCH", "PUT"])
@auth.require_auth
def update_annotation_endpoint(annotation_id):
    _require_str_id(annotation_id, "annotation_id")
    body = _body()
    ok = ann_ops.update_annotation(
        annotation_id,
        g.user_id,
        ann_type=body.get("type"),
        page=body.get("page"),
        data=body.get("data"),
    )
    if not ok:
        return jsonify(error="not found or not yours"), 404
    return jsonify(updated=True, annotation_id=annotation_id)


@app.delete("/annotations/<annotation_id>")
@auth.require_auth
def remove_annotation(annotation_id):
    _require_str_id(annotation_id, "annotation_id")
    ok = ann_ops.delete_annotation(annotation_id, g.user_id)
    if not ok:
        return jsonify(error="not found or not yours"), 404
    return jsonify(deleted=True)


@app.post("/annotations/<file_id>/export")
@auth.require_auth
def export_annotations(file_id):
    _check_owns(file_id)
    job_id = jobs.submit(ann_ops.export, owner_id=g.user_id,
                          source_file_id=file_id, file_id=file_id)
    return jsonify(job_id=job_id)


# ---------- documents & version history (Phase 6) ----------

@app.post("/documents")
@auth.require_auth
def create_document():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 300):
        raise ValueError("title must be a string up to 300 chars")
    doc_id = db.create_document(g.user_id, file_id, title=title)
    return jsonify(document_id=doc_id)


@app.get("/documents")
@auth.require_auth
def list_documents():
    return jsonify(owned=db.list_documents_for_user(g.user_id),
                    shared_with_me=db.list_documents_shared_with(g.user_id))


@app.get("/documents/<doc_id>")
@auth.require_auth
def get_document(doc_id):
    doc = _check_document_access(doc_id, required="view")
    current = db.get_document_version(doc_id, doc["current_version"])
    return jsonify(document=doc, current_version=current)


@app.post("/documents/<doc_id>/versions")
@auth.require_auth
def add_document_version(doc_id):
    _check_document_access(doc_id, required="edit")
    _check_document_not_locked_by_other(doc_id)
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    label = body.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 300):
        raise ValueError("label must be a string up to 300 chars")
    version_number = db.add_document_version(doc_id, file_id, g.user_id, label=label)
    return jsonify(version_number=version_number)


@app.get("/documents/<doc_id>/versions")
@auth.require_auth
def list_document_versions(doc_id):
    _check_document_access(doc_id, required="view")
    return jsonify(versions=db.list_document_versions(doc_id))


@app.post("/documents/<doc_id>/revert")
@auth.require_auth
def revert_document(doc_id):
    _check_document_access(doc_id, required="edit")
    _check_document_not_locked_by_other(doc_id)
    body = _body()
    ver_num = body["version_number"]
    if isinstance(ver_num, bool) or not isinstance(ver_num, int):
        raise ValueError("version_number must be an integer")
    target = db.get_document_version(doc_id, ver_num)
    if target is None:
        return jsonify(error="no such version"), 404
    new_version = db.add_document_version(
        doc_id, target["file_id"], g.user_id,
        label=f"reverted to v{ver_num}",
    )
    return jsonify(version_number=new_version, file_id=target["file_id"])


@app.post("/documents/<doc_id>/lock")
@auth.require_auth
def lock_document(doc_id):
    _check_document_access(doc_id, required="edit")
    acquired = db.set_document_lock(doc_id, g.user_id)
    if not acquired:
        return jsonify(error="document is locked by another session"), 409
    return jsonify(locked=True)


@app.post("/documents/<doc_id>/unlock")
@auth.require_auth
def unlock_document(doc_id):
    _check_document_access(doc_id, required="edit")
    released = db.release_document_lock(doc_id, g.user_id)
    if not released:
        return jsonify(error="you do not hold the lock on this document"), 409
    return jsonify(locked=False)


@app.post("/documents/<doc_id>/collaborators")
@auth.require_auth
def add_document_collaborator(doc_id):
    _check_owns_document(doc_id)
    body = _body()
    permission = body.get("permission", "view")
    if permission not in _PERMISSION_RANK:
        return jsonify(error="permission must be view, comment, or edit"), 400
    email = _validate_email(body.get("email", ""))
    user = db.get_user_by_email(email)
    if user is None:
        return jsonify(error="no user with that email"), 404
    if user["id"] == g.user_id:
        raise ValueError("you already own this document")
    db.add_collaborator(doc_id, user["id"], permission)
    return jsonify(added=True, user_id=user["id"], permission=permission)


@app.get("/documents/<doc_id>/collaborators")
@auth.require_auth
def get_document_collaborators(doc_id):
    _check_document_access(doc_id, required="view")
    return jsonify(collaborators=db.list_collaborators(doc_id))


@app.delete("/documents/<doc_id>/collaborators/<user_id>")
@auth.require_auth
def remove_document_collaborator(doc_id, user_id):
    _check_owns_document(doc_id)
    _require_str_id(user_id, "user_id")
    ok = db.remove_collaborator(doc_id, user_id)
    if not ok:
        return jsonify(error="that user is not a collaborator on this document"), 404
    return jsonify(removed=True)


# ---------- sharing (Phase 6) ----------

@app.post("/share")
@auth.require_auth
def create_share():
    body = _body()
    file_id = body["file_id"]
    _check_owns(file_id)
    permission = body.get("permission", "view")
    if permission not in ("view", "comment", "edit"):
        return jsonify(error="permission must be view, comment, or edit"), 400
    expires_at = None
    if body.get("expires_in_hours") is not None:
        raw_exp = body["expires_in_hours"]
        if isinstance(raw_exp, bool) or not isinstance(raw_exp, (int, float)):
            raise ValueError("expires_in_hours must be a finite number between 0 and 8760")
        h = float(raw_exp)
        if not math.isfinite(h) or not (0 < h <= 24 * 365):
            raise ValueError("expires_in_hours must be between 0 and 8760")
        expires_at = time.time() + h * 3600
    token = db.create_share_link(file_id, g.user_id, permission=permission,
                                  expires_at=expires_at)
    return jsonify(token=token, permission=permission)


@app.get("/files/<file_id>/shares")
@auth.require_auth
def list_shares(file_id):
    _check_owns(file_id)
    return jsonify(shares=db.list_share_links_for_file(file_id))


@app.delete("/share/<token>")
@auth.require_auth
def revoke_share(token):
    _require_str_id(token, "token")
    ok = db.revoke_share_link(token, g.user_id)
    if not ok:
        return jsonify(error="no such share link, or not yours"), 404
    return jsonify(revoked=True)


@app.get("/share/<token>")
def get_share_info(token):
    """Public — no auth. Returns only whitelisted file metadata (never owner_id)."""
    link = _resolve_share(token)
    meta = db.get_file(link["file_id"])
    safe_meta = {
        "original_name": meta.get("original_name"),
        "format": meta.get("format"),
        "size": meta.get("size"),
    } if meta else None
    return jsonify(
        file_id=link["file_id"],
        permission=link["permission"],
        original_name=safe_meta["original_name"] if safe_meta else None,
        format=safe_meta["format"] if safe_meta else None,
        size=safe_meta["size"] if safe_meta else None,
        meta=safe_meta,
    )


@app.get("/share/<token>/download")
def download_via_share(token):
    link = _resolve_share(token)
    meta = db.get_file(link["file_id"])
    path = storage.resolve(link["file_id"])
    dl_name = os.path.basename((meta or {}).get("original_name") or link["file_id"])
    return send_file(path, as_attachment=True, download_name=dl_name)


# ---------- comments (Phase 6) ----------

def _validate_comment_inputs(text, author_name=None, mentions=None, page=None):
    if not isinstance(text, str) or not (1 <= len(text.strip()) <= 5000):
        raise ValueError("comment text must be 1..5000 characters")
    if author_name is not None:
        if not isinstance(author_name, str) or not (1 <= len(author_name.strip()) <= 120):
            raise ValueError("author_name must be 1..120 characters")
    if mentions is not None:
        if not isinstance(mentions, list) or len(mentions) > 50 or not all(isinstance(m, str) and len(m) <= 254 for m in mentions):
            raise ValueError("mentions must be a list of strings")
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")


def _mask_email_in_str(s: str) -> str:
    if not isinstance(s, str):
        return ""
    def _repl(m):
        full = m.group(0)
        local = full.split("@", 1)[0]
        return f"{local[0]}***" if local else "***"
    return _MASK_EMAIL_RE.sub(_repl, s)


@app.post("/comments")
@auth.require_auth
def create_comment():
    """Authenticated path — requires at least 'comment' permission on the file."""
    body = _body()
    file_id = body["file_id"]
    _check_file_permission(file_id, required="comment")
    text = body.get("text")
    mentions = body.get("mentions")
    page = body.get("page")
    _validate_comment_inputs(text, mentions=mentions, page=page)
    comment_id = db.add_comment(
        file_id, g.user_id, g.user["email"], text.strip(),
        mentions_json=json.dumps(mentions) if mentions else None,
        page=page,
    )
    return jsonify(comment_id=comment_id)


@app.post("/share/<token>/comments")
def create_comment_via_share(token):
    link = _resolve_share(token, required_permissions=("comment", "edit"))
    body = _body()
    author_name = body.get("author_name", "Anonymous")
    text = body.get("text")
    mentions = body.get("mentions")
    page = body.get("page")
    _validate_comment_inputs(text, author_name=author_name, mentions=mentions, page=page)
    comment_id = db.add_comment(
        link["file_id"], None, author_name.strip(), text.strip(),
        mentions_json=json.dumps(mentions) if mentions else None,
        page=page,
    )
    return jsonify(comment_id=comment_id)


def _format_comments(rows, public: bool = False):
    out = []
    for r in rows:
        r = dict(r)
        raw_mentions = json.loads(r["mentions"]) if r.get("mentions") else []
        if public:
            is_guest = r.get("author_id") is None
            label = _mask_email_in_str(r.get("author_label") or "User")
            if is_guest and not label.endswith("(guest)"):
                label = f"{label} (guest)"
            out.append({
                "id": r["id"],
                "author_label": label,
                "is_guest": is_guest,
                "text": r["text"],
                "page": r.get("page"),
                "mentions": [_mask_email_in_str(m) for m in raw_mentions],
                "resolved": r.get("resolved", 0),
                "created_at": r.get("created_at"),
            })
        else:
            r["mentions"] = raw_mentions
            out.append(r)
    return out


@app.get("/comments/<file_id>")
@auth.require_auth
def list_comments(file_id):
    _check_file_permission(file_id, required="view")
    return jsonify(comments=_format_comments(db.list_comments(file_id), public=False))


@app.get("/share/<token>/comments")
def list_comments_via_share(token):
    link = _resolve_share(token)
    return jsonify(comments=_format_comments(db.list_comments(link["file_id"]), public=True))


@app.post("/comments/<comment_id>/resolve")
@auth.require_auth
def resolve_comment(comment_id):
    _require_str_id(comment_id, "comment_id")
    comment = db.get_comment(comment_id)
    if comment is None:
        return jsonify(error="no such comment"), 404
    is_author = comment.get("author_id") == g.user_id
    perm = db.get_file_permission(g.user_id, comment["file_id"])
    has_comment_perm = perm in ("owner", "edit", "comment")
    if not (is_author or has_comment_perm):
        raise PermissionError("you do not have permission to resolve this comment")
    db.resolve_comment(comment_id)
    return jsonify(resolved=True)


@app.delete("/comments/<comment_id>")
@auth.require_auth
def delete_comment(comment_id):
    _require_str_id(comment_id, "comment_id")
    ok = db.delete_comment(comment_id, g.user_id)
    if not ok:
        return jsonify(error="no such comment, or not yours to delete"), 404
    return jsonify(deleted=True)


# ---------- jobs ----------

@app.get("/jobs/<job_id>")
@auth.require_auth
def job_status(job_id):
    _require_str_id(job_id, "job_id")
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="no such job"), 404
    if job.get("owner_id") and job["owner_id"] != g.user_id:
        raise PermissionError("you do not have access to this job")
    return jsonify(
        id=job["id"],
        status=job["status"],
        result=job["result"],
        error=job["error"],
        created_at=job.get("created_at"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
