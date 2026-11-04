"""
Document metadata — reads and writes the standard PDF Info dictionary
(title, author, subject, keywords, creator) plus arbitrary custom
fields, via pikepdf.
"""
import os
import re
import tempfile
import pikepdf
from app import storage

STANDARD_FIELDS = ("title", "author", "subject", "keywords", "creator")
_RESERVED_INFO_NAMES = set(STANDARD_FIELDS) | {"creationdate", "moddate", "producer", "trapped"}
_CUSTOM_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def get_metadata(file_id):
    src = storage.resolve(file_id)
    with pikepdf.open(src) as pdf:
        docinfo = pdf.docinfo
        result = {}
        for field in STANDARD_FIELDS:
            key = f"/{field.capitalize()}"
            result[field] = str(docinfo[key]) if key in docinfo else None
        custom = {}
        for key in docinfo.keys():
            name = str(key).lstrip("/")
            if name.lower() not in _RESERVED_INFO_NAMES:
                custom[name] = str(docinfo[key])
        result["custom"] = custom
        return result


def validate_metadata_inputs(fields, custom):
    if fields is not None and not isinstance(fields, dict):
        raise ValueError("fields must be a JSON object")
    if custom is not None and not isinstance(custom, dict):
        raise ValueError("custom must be a JSON object")
    for field in (fields or {}).keys():
        if not isinstance(field, str) or field not in STANDARD_FIELDS:
            raise ValueError(f"unknown standard field: {field}")
    for key in (custom or {}).keys():
        if not isinstance(key, str) or not _CUSTOM_KEY_RE.match(key):
            raise ValueError(f"invalid custom metadata key {key!r}: must match ^[A-Za-z][A-Za-z0-9_-]{{0,63}}$")
        if key.lower() in _RESERVED_INFO_NAMES:
            raise ValueError(f"custom key {key!r} conflicts with standard PDF metadata field")


def set_metadata(file_id, fields: dict = None, custom: dict = None):
    """fields: any of title/author/subject/keywords/creator -> value
    (or None to remove). custom: arbitrary key -> value pairs."""
    validate_metadata_inputs(fields, custom)
    src = storage.resolve(file_id)
    with pikepdf.open(src) as pdf:
        docinfo = pdf.docinfo
        for field, value in (fields or {}).items():
            key = f"/{field.capitalize()}"
            if value is None:
                if key in docinfo:
                    del docinfo[key]
            else:
                docinfo[key] = str(value)
        for key, value in (custom or {}).items():
            pdf_key = f"/{key}"
            if value is None:
                if pdf_key in docinfo:
                    del docinfo[pdf_key]
            else:
                docinfo[pdf_key] = str(value)

        # Remove stale XMP metadata stream if present so viewers read updated /Info
        if "/Metadata" in pdf.Root:
            try:
                del pdf.Root["/Metadata"]
            except Exception:
                pass

        fd, out_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.save(out_path)
    return storage.save_output_path(out_path, ".pdf")
