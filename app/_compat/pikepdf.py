"""pikepdf-compatible library backed by pypdf and qpdf CLI."""
import io
import os
import subprocess
import tempfile
from PIL import Image
import pypdf
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)


class PdfError(Exception):
    pass


class PasswordError(PdfError):
    pass


def Name(val: str) -> NameObject:
    if not val.startswith("/"):
        val = "/" + val
    return NameObject(val)


def _to_pypdf_obj(val):
    if isinstance(val, _PikeWrapper):
        return val._ref if val._ref is not None else val._obj
    if isinstance(val, (
        NameObject, DictionaryObject, ArrayObject, StreamObject,
        IndirectObject, NumberObject, FloatObject, BooleanObject,
        TextStringObject, NullObject
    )):
        return val
    if isinstance(val, bool):
        return BooleanObject(val)
    if isinstance(val, int):
        return NumberObject(val)
    if isinstance(val, float):
        return FloatObject(val)
    if isinstance(val, str):
        return TextStringObject(val)
    if isinstance(val, list):
        return ArrayObject([_to_pypdf_obj(x) for x in val])
    if isinstance(val, dict):
        d = DictionaryObject()
        for k, v in val.items():
            nk = k if isinstance(k, NameObject) else Name(str(k))
            d[nk] = _to_pypdf_obj(v)
        return d
    if val is None:
        return NullObject()
    return val


def _wrap(obj, pdf=None, ref=None):
    if isinstance(obj, _PikeWrapper):
        return obj
    if isinstance(obj, IndirectObject):
        return _PikeWrapper(obj.get_object(), pdf=pdf, ref=obj)
    if isinstance(obj, (DictionaryObject, ArrayObject, StreamObject)):
        return _PikeWrapper(obj, pdf=pdf, ref=ref)
    return obj


class _PikeWrapper:
    def __init__(self, obj, pdf=None, ref=None):
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_pdf", pdf)
        object.__setattr__(self, "_ref", ref)

    def _deref(self):
        obj = object.__getattribute__(self, "_obj")
        if isinstance(obj, IndirectObject):
            return obj.get_object()
        return obj

    def __contains__(self, key):
        obj = self._deref()
        if isinstance(key, str) and not key.startswith("/"):
            key = "/" + key
        return NameObject(key) in obj if isinstance(key, str) else key in obj

    def __getitem__(self, key):
        obj = self._deref()
        if isinstance( obj, ArrayObject ):
            return _wrap(obj[key], pdf=self._pdf)
        k = Name(key) if isinstance(key, str) else key
        val = obj[k]
        return _wrap(val, pdf=self._pdf)

    def __setitem__(self, key, val):
        obj = self._deref()
        if isinstance(obj, ArrayObject):
            obj[key] = _to_pypdf_obj(val)
            return
        k = Name(key) if isinstance(key, str) else key
        obj[k] = _to_pypdf_obj(val)

    def __delitem__(self, key):
        obj = self._deref()
        k = Name(key) if isinstance(key, str) else key
        if k in obj:
            del obj[k]

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def keys(self):
        obj = self._deref()
        return obj.keys()

    def items(self):
        obj = self._deref()
        return [(k, self[k]) for k in obj.keys()]

    def append(self, item):
        obj = self._deref()
        obj.append(_to_pypdf_obj(item))

    def __len__(self):
        return len(self._deref())

    def __iter__(self):
        obj = self._deref()
        if isinstance(obj, ArrayObject):
            for x in obj:
                yield _wrap(x, pdf=self._pdf)
        else:
            for k in obj.keys():
                yield k

    def write(self, data: bytes, filter=None):
        obj = self._deref()
        if isinstance(obj, EncodedStreamObject):
            obj._data = data
        elif isinstance(obj, StreamObject):
            obj._data = data
        if filter is not None:
            self["/Filter"] = filter
        self["/Length"] = len(data)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        slash = "/" + name
        if slash in self:
            return self[slash]
        raise AttributeError(name)

    def __setattr__(self, name, val):
        if name.startswith("_"):
            object.__setattr__(self, name, val)
        else:
            self["/" + name] = val


def Dictionary(d: dict = None):
    obj = DictionaryObject()
    for k, v in (d or {}).items():
        nk = k if isinstance(k, NameObject) else Name(str(k))
        obj[nk] = _to_pypdf_obj(v)
    return _PikeWrapper(obj)


def Array(items: list = None):
    obj = ArrayObject([_to_pypdf_obj(x) for x in (items or [])])
    return _PikeWrapper(obj)


def Stream(pdf, data: bytes):
    s = DecodedStreamObject()
    s.set_data(data)
    ref = pdf._writer._add_object(s)
    return _PikeWrapper(s, pdf=pdf, ref=ref)


class PdfImage:
    def __init__(self, image_obj):
        self.obj = image_obj._deref() if isinstance(image_obj, _PikeWrapper) else image_obj

    def as_pil_image(self):
        flt = self.obj.get("/Filter")
        if isinstance(flt, ArrayObject) and len(flt) > 0:
            flt = flt[0]
        flt_str = str(flt) if flt else ""
        w = int(self.obj.get("/Width", 0))
        h = int(self.obj.get("/Height", 0))
        cs = str(self.obj.get("/ColorSpace", "/DeviceRGB"))
        if flt_str in ("/DCTDecode", "/JPXDecode"):
            raw = self.obj._data if isinstance(self.obj, EncodedStreamObject) else self.obj.get_data()
            return Image.open(io.BytesIO(raw))
        raw = self.obj.get_data()
        mode = "RGB" if "RGB" in cs else ("L" if "Gray" in cs else "CMYK")
        return Image.frombytes(mode, (w, h), raw)


class Permissions:
    def __init__(self, extract=True, print_lowres=True, print_highres=True,
                 modify_annotation=True, modify_assembly=True,
                 modify_form=True, modify_other=True, accessibility=True):
        self.extract = bool(extract)
        self.print_lowres = bool(print_lowres)
        self.print_highres = bool(print_highres)
        self.modify_annotation = bool(modify_annotation)
        self.modify_assembly = bool(modify_assembly)
        self.modify_form = bool(modify_form)
        self.modify_other = bool(modify_other)
        self.accessibility = bool(accessibility)


class Encryption:
    def __init__(self, user="", owner="", R=6, allow=None):
        self.user = user or ""
        self.owner = owner or ""
        self.R = R
        self.allow = allow or Permissions()


class _DocInfoWrapper:
    def __init__(self, info_dict):
        self._info = info_dict

    def _norm(self, key):
        k = str(key)
        return NameObject(k if k.startswith("/") else "/" + k)

    def __contains__(self, key):
        return self._norm(key) in self._info

    def __getitem__(self, key):
        return self._info[self._norm(key)]

    def __setitem__(self, key, val):
        self._info[self._norm(key)] = TextStringObject(str(val))

    def __delitem__(self, key):
        nk = self._norm(key)
        if nk in self._info:
            del self._info[nk]

    def keys(self):
        return list(self._info.keys())


class Pdf:
    def __init__(self, src, password=""):
        self._password = password or ""
        self._tmp_input = None
        if isinstance(src, (str, os.PathLike)):
            self._src_path = str(src)
            with builtins_open(self._src_path, "rb") as f:
                raw_bytes = f.read()
        elif hasattr(src, "read"):
            pos = src.tell() if hasattr(src, "tell") else None
            raw_bytes = src.read()
            if pos is not None and hasattr(src, "seek"):
                src.seek(pos)
            fd, tmp_p = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            with builtins_open(tmp_p, "wb") as f:
                f.write(raw_bytes)
            self._src_path = tmp_p
            self._tmp_input = tmp_p
        else:
            raise PdfError("Unsupported source type")

        if not raw_bytes or b"%PDF-" not in raw_bytes[:1024]:
            self._cleanup()
            raise PdfError("Not a valid PDF file")

        # Check encryption via qpdf
        enc_check = subprocess.run(
            ["qpdf", f"--password={self._password}", "--show-encryption", self._src_path],
            capture_output=True, text=True
        )
        if enc_check.returncode == 2:
            err_lower = (enc_check.stderr or "").lower()
            self._cleanup()
            if "invalid password" in err_lower or "password" in err_lower:
                raise PasswordError("Invalid password")
            raise PdfError(enc_check.stderr.strip() or "Failed to open PDF")

        out_text = enc_check.stdout or ""
        self.is_encrypted = ("not encrypted" not in out_text.lower()) and ("R =" in out_text)
        self.allow = self._parse_permissions(out_text) if self.is_encrypted else Permissions()

        # Decrypt to a temporary buffer if encrypted so pypdf can read & write objects cleanly
        if self.is_encrypted:
            fd, dec_p = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            dec_proc = subprocess.run(
                ["qpdf", f"--password={self._password}", "--decrypt", self._src_path, dec_p],
                check=False, capture_output=True, text=True
            )
            if dec_proc.returncode == 2 or not os.path.exists(dec_p) or os.path.getsize(dec_p) == 0:
                if os.path.exists(dec_p):
                    os.remove(dec_p)
                self._cleanup()
                err_l = (dec_proc.stderr or "").lower()
                if "password" in err_l:
                    raise PasswordError("Invalid password")
                raise PdfError(dec_proc.stderr.strip() or "Failed to decrypt PDF")
            with builtins_open(dec_p, "rb") as f:
                work_bytes = f.read()
            os.remove(dec_p)
        else:
            work_bytes = raw_bytes

        try:
            self._reader = pypdf.PdfReader(io.BytesIO(work_bytes), strict=False)
            self._writer = pypdf.PdfWriter()
            self._writer.clone_document_from_reader(self._reader)
            # Copy existing /Info metadata into writer._info
            if self._writer._info is None:
                self._writer.add_metadata({})
            info_obj = self._writer._info.get_object()
            info_obj.clear()
            if self._reader.metadata:
                for k, v in self._reader.metadata.items():
                    if v is not None:
                        info_obj[NameObject(str(k))] = TextStringObject(str(v))
        except Exception as e:
            self._cleanup()
            raise PdfError(f"Failed to parse PDF: {e}") from e

    @staticmethod
    def _parse_permissions(text: str) -> Permissions:
        def _allowed(label):
            for line in text.splitlines():
                if line.strip().lower().startswith(label):
                    return "not allowed" not in line.lower()
            return True
        return Permissions(
            extract=_allowed("extract for any purpose:"),
            print_lowres=_allowed("print low resolution:"),
            print_highres=_allowed("print high resolution:"),
            modify_assembly=_allowed("modify document assembly:"),
            modify_form=_allowed("modify forms:"),
            modify_annotation=_allowed("modify annotations:"),
            modify_other=_allowed("modify other:"),
        )

    @property
    def pages(self):
        return [_wrap(p, pdf=self) for p in self._writer.pages]

    @property
    def Root(self):
        return _wrap(self._writer._root_object, pdf=self)

    @property
    def docinfo(self):
        return _DocInfoWrapper(self._writer._info.get_object())

    def make_indirect(self, obj):
        raw = obj._obj if isinstance(obj, _PikeWrapper) else _to_pypdf_obj(obj)
        ref = self._writer._add_object(raw)
        return _PikeWrapper(raw, pdf=self, ref=ref)

    def save(self, dest, encryption: Encryption = None):
        buf = io.BytesIO()
        self._writer.write(buf)
        raw_out = buf.getvalue()

        if encryption is not None:
            fd1, p_in = tempfile.mkstemp(suffix=".pdf")
            fd2, p_out = tempfile.mkstemp(suffix=".pdf")
            os.close(fd1)
            os.close(fd2)
            try:
                with builtins_open(p_in, "wb") as f:
                    f.write(raw_out)
                perms = encryption.allow
                extract_flag = "y" if perms.extract else "n"
                if perms.print_highres:
                    print_flag = "full"
                elif perms.print_lowres:
                    print_flag = "low"
                else:
                    print_flag = "none"
                if perms.modify_other:
                    modify_flag = "all"
                elif perms.modify_annotation:
                    modify_flag = "annotate"
                elif perms.modify_form:
                    modify_flag = "form"
                elif perms.modify_assembly:
                    modify_flag = "assembly"
                else:
                    modify_flag = "none"

                cmd = [
                    "qpdf", "--encrypt",
                    encryption.user, encryption.owner, "256",
                    f"--extract={extract_flag}",
                    f"--print={print_flag}",
                    f"--modify={modify_flag}",
                    "--", p_in, p_out
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode not in (0, 3) or not os.path.exists(p_out):
                    raise PdfError(f"qpdf encrypt failed: {proc.stderr}")
                with builtins_open(p_out, "rb") as f:
                    raw_out = f.read()
            finally:
                for p in (p_in, p_out):
                    if os.path.exists(p):
                        os.remove(p)

        if isinstance(dest, (str, os.PathLike)):
            with builtins_open(dest, "wb") as f:
                f.write(raw_out)
        elif hasattr(dest, "write"):
            dest.write(raw_out)
        else:
            raise TypeError("Unsupported destination for save()")

    def _cleanup(self):
        if getattr(self, "_tmp_input", None) and os.path.exists(self._tmp_input):
            try:
                os.remove(self._tmp_input)
            except OSError:
                pass
            self._tmp_input = None

    def close(self):
        self._cleanup()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


builtins_open = open


def open(src, password=""):
    return Pdf(src, password=password)
