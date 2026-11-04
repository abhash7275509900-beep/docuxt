FROM python:3.12-slim

# System deps:
#   qpdf              - PDF compression (Phase 1)
#   libjpeg62-turbo   - JPEG support for image recompression (Phase 1)
#   libreoffice       - DOCX/XLSX/PPTX <-> PDF conversion (Phase 3)
#   tesseract-ocr     - OCR & OSD script detection (Phase 4)
#   fonts-dejavu-core - Unicode TrueType font for watermarks/annotations
#   fonts-noto-cjk    - CJK font support
RUN apt-get update && apt-get install -y --no-install-recommends \
        qpdf \
        libjpeg62-turbo \
        libreoffice \
        tesseract-ocr \
        tesseract-ocr-eng \
        fonts-dejavu-core \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend

RUN mkdir -p storage/uploads storage/outputs

ENV DOCENGINE_ENV=production
EXPOSE 8000
ENV PORT=8000

RUN pip install --no-cache-dir gunicorn
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:${PORT:-8000} app.main:app"]
