"""
File storage abstraction.

Local disk under storage/uploads and storage/outputs.
Sanitizes extensions, prevents path traversal, and supports file deletion.
"""
import os
import re
import uuid
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "storage", "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "storage", "outputs")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

_SAFE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def _sanitize_ext(ext: str) -> str:
    ext = (ext or "").lower().strip()
    if _SAFE_EXT_RE.match(ext):
        return ext
    return ""


def _new_id(ext):
    return f"{uuid.uuid4().hex}{_sanitize_ext(ext)}"


def save_upload_stream(file_storage, original_filename):
    """Save a werkzeug FileStorage object. Returns file_id."""
    ext = _sanitize_ext(os.path.splitext(original_filename or "")[1])
    file_id = _new_id(ext)
    path = os.path.join(UPLOAD_DIR, file_id)
    file_storage.save(path)
    return file_id


def save_output_path(src_path, ext):
    """Move a locally-produced file into the outputs dir. Returns file_id."""
    file_id = _new_id(ext)
    dest = os.path.join(OUTPUT_DIR, file_id)
    shutil.move(src_path, dest)
    return file_id


def save_output_bytes(data: bytes, ext):
    file_id = _new_id(ext)
    dest = os.path.join(OUTPUT_DIR, file_id)
    with open(dest, "wb") as f:
        f.write(data)
    return file_id


def resolve(file_id):
    """Find the absolute path for a file_id, checking both dirs."""
    if (
        not isinstance(file_id, str)
        or not file_id
        or os.path.basename(file_id) != file_id
        or file_id in (".", "..")
    ):
        raise FileNotFoundError("file not found")
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        p = os.path.join(d, file_id)
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("file not found")


def delete_file(file_id) -> bool:
    """Remove a stored file from disk if present."""
    if (
        not isinstance(file_id, str)
        or not file_id
        or os.path.basename(file_id) != file_id
        or file_id in (".", "..")
    ):
        return False
    removed = False
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        p = os.path.join(d, file_id)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed = True
            except OSError:
                pass
    return removed


def size_of(file_id):
    return os.path.getsize(resolve(file_id))
