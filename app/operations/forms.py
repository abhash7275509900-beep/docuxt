"""
Forms — Phase 5.

Field creation uses pikepdf at the raw PDF-object level; filling and reading
go through pypdf while preserving bookmarks, metadata, and dynamic checkbox
appearance states.
"""
import os
import tempfile
import pikepdf
from pypdf import PdfReader, PdfWriter

from app import storage
from app.operations import (
    parse_bool,
    parse_finite_float,
    copy_metadata_and_forms,
    displayed_to_user_rect,
)

ALLOWED_FIELD_TYPES = {"text", "checkbox"}


def _escape_pdf_string(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _font_resources(pdf):
    return pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/Helv": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
                "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
            })
        })
    })


def _text_appearance(pdf, width, height, value, font_size=10):
    y = max(2, (height - font_size) / 2)
    stream_data = (
        f"/Tx BMC q BT /Helv {font_size} Tf 0 g 2 {y:.2f} Td "
        f"({_escape_pdf_string(value)}) Tj ET Q EMC"
    ).encode("latin-1", errors="replace")
    xobj = pikepdf.Stream(pdf, stream_data)
    xobj["/Type"] = pikepdf.Name("/XObject")
    xobj["/Subtype"] = pikepdf.Name("/Form")
    xobj["/BBox"] = pikepdf.Array([0, 0, width, height])
    xobj["/Resources"] = _font_resources(pdf)
    return xobj


def _checkbox_appearance(pdf, width, height, checked):
    if checked:
        m = 2
        data = f"q 0 g {m} {m} {width - 2*m} {height - 2*m} re f Q".encode()
    else:
        data = b""
    xobj = pikepdf.Stream(pdf, data)
    xobj["/Type"] = pikepdf.Name("/XObject")
    xobj["/Subtype"] = pikepdf.Name("/Form")
    xobj["/BBox"] = pikepdf.Array([0, 0, width, height])
    return xobj


def _parse_checkbox_bool(val) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        s = val.strip()
        if s in ("/Yes", "Yes", "yes", "true", "True", "1", "/On", "On", "on", "/Checked", "Checked"):
            return True
        if s in ("/Off", "Off", "off", "false", "False", "0", ""):
            return False
    return parse_bool(val, "checkbox value")


def _get_checkbox_on_state(f_obj) -> str:
    """Inspect field /_States_ or widget /AP /N keys for the non-/Off appearance state name."""
    try:
        states = f_obj.get("/_States_")
        if states:
            for st in states:
                ks = str(st)
                if ks != "/Off":
                    return ks if ks.startswith("/") else f"/{ks}"
        ap = f_obj.get("/AP")
        if ap:
            ap_obj = ap.get_object() if hasattr(ap, "get_object") else ap
            n_dict = ap_obj.get("/N")
            if n_dict:
                n_obj = n_dict.get_object() if hasattr(n_dict, "get_object") else n_dict
                for k in n_obj.keys():
                    ks = str(k)
                    if ks != "/Off":
                        return ks if ks.startswith("/") else f"/{ks}"
    except Exception:
        pass
    return "/Yes"


def create_form(file_id, fields: list):
    """fields: [{name, type: 'text'|'checkbox', page (1-indexed),
    x, y, width, height, value?}, ...]. Returns a new file_id with the
    fillable fields embedded."""
    if not isinstance(fields, list) or not fields:
        raise ValueError("fields must be a non-empty list")
    for spec in fields:
        if not isinstance(spec, dict):
            raise ValueError("each field spec must be an object")
        if spec.get("type", "text") not in ALLOWED_FIELD_TYPES:
            raise ValueError(f"field type must be one of {sorted(ALLOWED_FIELD_TYPES)}")
        if not isinstance(spec.get("name"), str) or not spec["name"].strip():
            raise ValueError("field name must be a non-empty string")

    names = [s["name"].strip() for s in fields]
    if len(set(names)) != len(names):
        raise ValueError("duplicate field names in request")

    src = storage.resolve(file_id)
    pypdf_reader = PdfReader(src)
    existing = set((pypdf_reader.get_fields() or {}).keys())
    clash = sorted(set(names) & existing)
    if clash:
        raise ValueError(f"field name(s) already exist in PDF: {', '.join(clash)}")
    with pikepdf.open(src) as pdf:
        n_pages = len(pdf.pages)
        if "/AcroForm" not in pdf.Root:
            pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary({
                "/Fields": pikepdf.Array([]),
                "/DA": "/Helv 10 Tf 0 g",
                "/DR": _font_resources(pdf),
                "/NeedAppearances": True,
            }))
        acroform = pdf.Root.AcroForm

        for spec in fields:
            page_idx = spec.get("page")
            if isinstance(page_idx, bool) or not isinstance(page_idx, int) or not (1 <= page_idx <= n_pages):
                raise ValueError(f"field page {page_idx!r} must be an integer 1..{n_pages}")
            page = pdf.pages[page_idx - 1]
            pypdf_page = pypdf_reader.pages[page_idx - 1]
            x = parse_finite_float(spec.get("x"), "x")
            y = parse_finite_float(spec.get("y"), "y")
            w = parse_finite_float(spec.get("width"), "width", min_val=0.0, exclusive_min=True)
            h = parse_finite_float(spec.get("height"), "height", min_val=0.0, exclusive_min=True)
            ux0, uy0, ux1, uy1 = displayed_to_user_rect(pypdf_page, x, y, w, h)
            uw, uh = ux1 - ux0, uy1 - uy0
            field_type = spec.get("type", "text")

            widget = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Widget"),
                "/Rect": pikepdf.Array([ux0, uy0, ux1, uy1]),
                "/T": spec["name"],
                "/F": 4,
            }))

            if field_type == "text":
                value = "" if spec.get("value") is None else str(spec.get("value"))
                widget["/FT"] = pikepdf.Name("/Tx")
                widget["/V"] = value
                widget["/DA"] = "/Helv 10 Tf 0 g"
                ap = pdf.make_indirect(pikepdf.Dictionary({}))
                ap["/N"] = _text_appearance(pdf, uw, uh, value)
                widget["/AP"] = ap
            else:  # checkbox
                checked = _parse_checkbox_bool(spec.get("value", False))
                state = pikepdf.Name("/Yes") if checked else pikepdf.Name("/Off")
                widget["/FT"] = pikepdf.Name("/Btn")
                widget["/V"] = state
                widget["/AS"] = state
                n_dict = pdf.make_indirect(pikepdf.Dictionary({}))
                n_dict["/Yes"] = _checkbox_appearance(pdf, uw, uh, True)
                n_dict["/Off"] = _checkbox_appearance(pdf, uw, uh, False)
                ap = pdf.make_indirect(pikepdf.Dictionary({}))
                ap["/N"] = n_dict
                widget["/AP"] = ap

            if "/Annots" in page:
                page.Annots.append(widget)
            else:
                page["/Annots"] = pikepdf.Array([widget])
            acroform.Fields.append(widget)

        fd, out_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.save(out_path)

    return storage.save_output_path(out_path, ".pdf")


def fill_form(file_id, data: dict):
    """data: {field_name: value}. Validates field names against the PDF's
    actual AcroForm fields and returns a new file_id."""
    if not isinstance(data, dict) or not data:
        raise ValueError("data must be a non-empty object")
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    existing_fields = reader.get_fields() or {}
    if not existing_fields:
        raise ValueError("PDF has no form fields")

    unknown = [k for k in data.keys() if k not in existing_fields]
    if unknown:
        raise ValueError(f"unknown form field(s): {', '.join(sorted(map(str, unknown)))}")

    normalized_data = {}
    for k, v in data.items():
        f_obj = existing_fields[k]
        if f_obj.get("/FT") == "/Btn" and (int(f_obj.get("/Ff", 0) or 0) & 32768):
            opt = "" if v is None else str(v).strip()
            normalized_data[k] = opt if opt.startswith("/") else f"/{opt}"
        elif f_obj.get("/FT") == "/Btn":
            checked = _parse_checkbox_bool(v)
            on_state = _get_checkbox_on_state(f_obj)
            normalized_data[k] = on_state if checked else "/Off"
        else:
            normalized_data[k] = "" if v is None else str(v)

    writer = PdfWriter(clone_from=src)
    writer.set_need_appearances_writer(True)
    for page in writer.pages:
        writer.update_page_form_field_values(page, normalized_data)
    copy_metadata_and_forms(reader, writer, copy_forms=False)

    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out_path, "wb") as f:
        writer.write(f)
    return storage.save_output_path(out_path, ".pdf")


def extract_form_data(file_id):
    src = storage.resolve(file_id)
    reader = PdfReader(src)
    fields = reader.get_fields() or {}
    out = {}
    for name, field in fields.items():
        value = field.get("/V")
        out[name] = str(value) if value is not None else None
    return out
