# Python version can be changed, e.g.
# FROM python:3.8
# FROM ghcr.io/mamba-org/micromamba:1.5.1-focal-cuda-11.3.1
FROM docker.io/python:3.12.1-slim-bookworm

LABEL org.opencontainers.image.authors="FNNDSC <dev@babyMRI.org>" \
      org.opencontainers.image.title="A ChRIS plugin to detect and redact PHI embedded in DICOM pixel data" \
      org.opencontainers.image.description="A ChRIS plugin to detect and redact PHI embedded in DICOM pixel data using Microsoft Presidio"

# Tesseract OCR is required by presidio-image-redactor for burned-in text detection.
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr libgl1 && \
    rm -rf /var/lib/apt/lists/*

ARG SRCDIR=/usr/local/src/pl-dicom_pixel_redact
WORKDIR ${SRCDIR}

COPY requirements.txt .
RUN --mount=type=cache,sharing=private,target=/root/.cache/pip pip install -r requirements.txt

COPY . .
ARG extras_require=none
RUN pip install ".[${extras_require}]" && \
    python -m spacy download en_core_web_lg \
    && cd / && rm -rf ${SRCDIR}
WORKDIR /

CMD ["dicom_pixel_redact"]
