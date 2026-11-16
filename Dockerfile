FROM python:3.12.14-slim-bookworm AS wheels
WORKDIR /build
COPY requirements.txt constraints.txt .
RUN --mount=type=secret,id=build_ca,target=/run/secrets/build-ca.pem \
    if [ -f /run/secrets/build-ca.pem ]; then PIP_CERT=/run/secrets/build-ca.pem pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt; else pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt; fi

FROM python:3.12.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Amsterdam
WORKDIR /app
COPY --chown=568:568 . .
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-nld libzbar0 libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --no-index /wheels/* \
    && groupadd -g 568 chrysant && useradd -u 568 -g 568 -m chrysant \
    && mkdir -p /data /app && chown 568:568 /data /app
USER 568:568
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
