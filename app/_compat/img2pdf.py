"""img2pdf implementation embedding JPEG streams directly and PNG/other via PIL/pypdf."""
import io
import zlib
from PIL import Image


class AlphaChannelError(Exception):
    pass


def get_layout_fun(pagesize=None, *args, **kwargs):
    def _layout(imgwidthpx, imgheightpx, ndpi):
        if pagesize is not None:
            pw, ph = pagesize
            return (pw, ph, pw, ph)
        dpi_x = ndpi[0] if ndpi and ndpi[0] else 72.0
        dpi_y = ndpi[1] if ndpi and ndpi[1] else 72.0
        pw = imgwidthpx * 72.0 / dpi_x
        ph = imgheightpx * 72.0 / dpi_y
        return (pw, ph, pw, ph)
    return _layout


def _read_item_bytes(item):
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    if isinstance(item, str):
        with open(item, "rb") as f:
            return f.read()
    if hasattr(item, "read"):
        return item.read()
    raise TypeError(f"Unsupported input type: {type(item)}")


def _build_pdf_with_images(items, layout_fun=None):
    pages_data = []
    for item in items:
        raw = _read_item_bytes(item)
        img = Image.open(io.BytesIO(raw))
        dpi = img.info.get("dpi", (72.0, 72.0))
        if not dpi or dpi[0] <= 0 or dpi[1] <= 0:
            dpi = (72.0, 72.0)
        w_px, h_px = img.size
        if layout_fun is not None:
            res = layout_fun(w_px, h_px, dpi)
            if len(res) == 4:
                page_w, page_h, img_w, img_h = res
            else:
                page_w, page_h = res[0], res[1]
                img_w, img_h = page_w, page_h
        else:
            page_w = w_px * 72.0 / float(dpi[0])
            page_h = h_px * 72.0 / float(dpi[1])
            img_w, img_h = page_w, page_h

        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            stream_bytes = raw
            pdf_filter = "/DCTDecode"
            colorspace = "/DeviceRGB" if img.mode == "RGB" else "/DeviceGray"
        else:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                rgb = bg
            elif img.mode != "RGB":
                rgb = img.convert("RGB")
            else:
                rgb = img
            stream_bytes = zlib.compress(rgb.tobytes())
            pdf_filter = "/FlateDecode"
            colorspace = "/DeviceRGB"

        pages_data.append({
            "w_px": w_px,
            "h_px": h_px,
            "page_w": float(page_w),
            "page_h": float(page_h),
            "img_w": float(img_w),
            "img_h": float(img_h),
            "stream": stream_bytes,
            "filter": pdf_filter,
            "cs": colorspace,
        })

    objs = {}
    kid_refs = []
    for i, p in enumerate(pages_data):
        page_oid = 3 + i * 3
        content_oid = 4 + i * 3
        img_oid = 5 + i * 3
        kid_refs.append(f"{page_oid} 0 R")

        x_off = (p["page_w"] - p["img_w"]) / 2.0
        y_off = (p["page_h"] - p["img_h"]) / 2.0
        content_bytes = f"q {p['img_w']:.4f} 0 0 {p['img_h']:.4f} {x_off:.4f} {y_off:.4f} cm /Im0 Do Q\n".encode("ascii")

        objs[page_oid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {p['page_w']:.4f} {p['page_h']:.4f}] "
            f"/Resources << /XObject << /Im0 {img_oid} 0 R >> >> /Contents {content_oid} 0 R >>"
        ).encode("ascii")

        objs[content_oid] = (
            f"<< /Length {len(content_bytes)} >>\nstream\n".encode("ascii")
            + content_bytes
            + b"\nendstream"
        )

        objs[img_oid] = (
            f"<< /Type /XObject /Subtype /Image /Width {p['w_px']} /Height {p['h_px']} "
            f"/ColorSpace {p['cs']} /BitsPerComponent 8 /Filter {p['filter']} /Length {len(p['stream'])} >>\nstream\n".encode("ascii")
            + p["stream"]
            + b"\nendstream"
        )

    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kid_refs)}] /Count {len(pages_data)} >>".encode("ascii")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    max_oid = max(objs.keys())
    for oid in range(1, max_oid + 1):
        offsets[oid] = out.tell()
        out.write(f"{oid} 0 obj\n".encode("ascii"))
        out.write(objs[oid])
        out.write(b"\nendobj\n")

    xref_pos = out.tell()
    out.write(f"xref\n0 {max_oid + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for oid in range(1, max_oid + 1):
        out.write(f"{offsets[oid]:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {max_oid + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return out.getvalue()


def convert(*images, layout_fun=None, **kwargs):
    if len(images) == 1 and isinstance(images[0], (list, tuple)):
        items = list(images[0])
    else:
        items = list(images)
    return _build_pdf_with_images(items, layout_fun=layout_fun)
