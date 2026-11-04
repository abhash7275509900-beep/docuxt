"""
Cross-format document conversion — Phase 3.

Backed by LibreOffice headless (`soffice --convert-to`) with isolated
per-conversion UserInstallation profiles so multiple conversions run
concurrently without lock contention.
Validates source->target format compatibility up front and delegates
PDF->XLSX / PDF->CSV table conversions to `tables.extract_tables`.
"""
import os
import shutil
import signal
import subprocess
import tempfile

from app import storage
from app.operations import tables as tables_ops

FORMAT_FILTERS = {
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
    "odt": "odt",
    "rtf": "rtf",
    "txt": "txt:Text",
    "html": "html",
    "xlsx": "xlsx",
    "xls": "xls",
    "ods": "ods",
    "csv": "csv:Text - txt - csv (StarCalc)",
    "pptx": "pptx",
    "ppt": "ppt",
    "odp": "odp",
}

COMPATIBLE_TARGETS = {
    ".pdf": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html", "xlsx", "csv"],
    ".docx": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".doc": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".odt": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".rtf": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".txt": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".html": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html"],
    ".xlsx": ["pdf", "xlsx", "xls", "ods", "csv", "html"],
    ".xls": ["pdf", "xlsx", "xls", "ods", "csv", "html"],
    ".ods": ["pdf", "xlsx", "xls", "ods", "csv", "html"],
    ".csv": ["pdf", "xlsx", "xls", "ods", "csv", "html"],
    ".pptx": ["pdf", "pptx", "ppt", "odp"],
    ".ppt": ["pdf", "pptx", "ppt", "odp"],
    ".odp": ["pdf", "pptx", "ppt", "odp"],
}

CONVERSION_TIMEOUT_SECONDS = 120
PDF_INPUT_FILTER = "writer_pdf_import"


def validate_conversion_pair(src_ext: str, target_format: str) -> str:
    if not isinstance(target_format, str):
        raise ValueError("target_format must be a string")
    target_format = target_format.lower().lstrip(".")
    if target_format not in FORMAT_FILTERS:
        raise ValueError(
            f"unsupported target format: {target_format}. Supported: {sorted(FORMAT_FILTERS)}"
        )
    src_ext = (src_ext or "").lower()
    if src_ext in COMPATIBLE_TARGETS:
        allowed = COMPATIBLE_TARGETS[src_ext]
        if target_format not in allowed:
            raise ValueError(
                f"cannot convert {src_ext} to {target_format}; allowed targets for {src_ext}: {allowed}"
            )
    return target_format


def convert(file_id, target_format: str):
    """Convert file_id to target_format (e.g. 'pdf', 'docx', 'xlsx')."""
    src_path = storage.resolve(file_id)
    src_ext = os.path.splitext(src_path)[1].lower()
    target_format = validate_conversion_pair(src_ext, target_format)

    # PDF -> XLSX / CSV uses table extraction rather than Writer import
    if src_ext == ".pdf" and target_format in ("xlsx", "csv"):
        res = tables_ops.extract_tables(file_id, fmt=target_format)
        return res["file_id"]

    work_dir = tempfile.mkdtemp(prefix="docengine_convert_")
    profile_dir = tempfile.mkdtemp(prefix="docengine_lo_profile_")

    try:
        clean_input = os.path.join(work_dir, f"input{src_ext}")
        shutil.copyfile(src_path, clean_input)

        filter_spec = FORMAT_FILTERS[target_format]
        cmd = [
            "soffice",
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile_dir}",
        ]
        if src_ext == ".pdf":
            cmd.append(f"--infilter={PDF_INPUT_FILTER}")
        cmd += ["--convert-to", filter_spec, "--outdir", work_dir, clean_input]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            proc.communicate(timeout=CONVERSION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            raise RuntimeError("LibreOffice conversion timed out") from e

        out_ext = filter_spec.split(":")[0]
        expected_output = os.path.join(work_dir, f"input.{out_ext}")

        if not os.path.exists(expected_output):
            raise RuntimeError(f"LibreOffice conversion failed (exit {proc.returncode})")

        return storage.save_output_path(expected_output, f".{out_ext}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
