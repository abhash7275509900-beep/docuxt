"""
PDF compression.

Two layers:
1. Structural (always applied): qpdf recompresses object streams,
   removes redundant objects. Lossless.
2. Image recompression (quality-dependent): walk embedded images via
   pikepdf, skip transparent/mask/stencil or tiny images, deduplicate
   shared XObjects, and only replace an image stream when the new JPEG
   is genuinely smaller than the original stream.
   Never returns an output PDF larger than the input PDF.
"""
import io
import os
import shutil
import subprocess
import tempfile
import pikepdf
from PIL import Image
from app import storage

PRESETS = {
    "low": {"jpeg_quality": 85, "max_dim": 2000},     # light compression
    "medium": {"jpeg_quality": 60, "max_dim": 1500},
    "high": {"jpeg_quality": 35, "max_dim": 1000},     # aggressive
}


def compress(file_id, quality: str = "medium"):
    if quality not in PRESETS:
        raise ValueError(f"quality must be one of {list(PRESETS)}")
    preset = PRESETS[quality]

    src = storage.resolve(file_id)
    before = os.path.getsize(src)

    fd1, tmp_recompressed = tempfile.mkstemp(suffix=".pdf")
    os.close(fd1)
    fd2, tmp_final = tempfile.mkstemp(suffix=".pdf")
    os.close(fd2)

    try:
        # Step 1: recompress images in place using pikepdf
        _recompress_images(src, tmp_recompressed, preset)

        # Step 2: qpdf structural/stream compression pass
        # Note: qpdf exit code 3 indicates warnings with valid output produced.
        proc = subprocess.run(
            [
                "qpdf",
                "--compress-streams=y",
                "--object-streams=generate",
                "--recompress-flate",
                tmp_recompressed,
                tmp_final,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode not in (0, 3) or not os.path.exists(tmp_final) or os.path.getsize(tmp_final) == 0:
            raise RuntimeError("PDF compression failed")

        # Never return a compressed file larger than the original input
        already_optimal = False
        if os.path.getsize(tmp_final) >= before:
            shutil.copyfile(src, tmp_final)
            already_optimal = True

        out_id = storage.save_output_path(tmp_final, ".pdf")
        after = storage.size_of(out_id)

        res = {
            "file_id": out_id,
            "size_before": before,
            "size_after": after,
            "ratio": max(0.0, round(1 - after / before, 3)) if before else 0.0,
        }
        if already_optimal:
            res["note"] = "already optimal"
        return res
    finally:
        for p in (tmp_recompressed, tmp_final):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _recompress_images(src_path, dest_path, preset):
    try:
        pdf = pikepdf.open(src_path)
    except Exception as e:
        raise ValueError("not a valid PDF file") from e
    seen_objgens = set()
    try:
        for page in pdf.pages:
            if "/Resources" not in page or "/XObject" not in page["/Resources"]:
                continue
            xobjects = page["/Resources"]["/XObject"]
            for name in list(xobjects.keys()):
                obj = xobjects[name]
                if obj.get("/Subtype") != pikepdf.Name("/Image"):
                    continue
                objgen = getattr(obj, "objgen", None)
                if objgen and objgen != (0, 0):
                    if objgen in seen_objgens:
                        continue
                    seen_objgens.add(objgen)
                try:
                    _recompress_one_image(pdf, obj, preset)
                except Exception:
                    continue
        pdf.save(dest_path)
    finally:
        pdf.close()


def _recompress_one_image(pdf, image_obj, preset):
    # Preserve stencil masks and transparent images (/SMask, /Mask) untouched
    if bool(image_obj.get("/ImageMask")):
        return
    if "/SMask" in image_obj or "/Mask" in image_obj or "/Decode" in image_obj:
        return

    try:
        old_raw = image_obj.read_raw_bytes()
        old_len = len(old_raw)
    except Exception:
        old_len = None

    if old_len is not None and old_len < 1024:
        return

    raw_image = pikepdf.PdfImage(image_obj)
    pil_image = raw_image.as_pil_image()

    # Do not flatten images with alpha/transparency channels
    if pil_image.mode in ("RGBA", "LA", "PA", "1") or (
        pil_image.mode == "P" and "transparency" in pil_image.info
    ):
        return

    w, h = pil_image.size
    if w * h < 64 * 64:
        return

    max_dim = preset["max_dim"]
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        pil_image = pil_image.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.LANCZOS,
        )

    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")

    buf = io.BytesIO()
    pil_image.save(buf, format="JPEG", quality=preset["jpeg_quality"], optimize=True)
    new_bytes = buf.getvalue()

    # Only replace the stream if the new JPEG stream is smaller than the original
    if old_len is not None and len(new_bytes) >= old_len:
        return

    image_obj.write(new_bytes, filter=pikepdf.Name("/DCTDecode"))
    image_obj.Width = pil_image.width
    image_obj.Height = pil_image.height
    image_obj.ColorSpace = pikepdf.Name("/DeviceRGB")
    image_obj.BitsPerComponent = 8
