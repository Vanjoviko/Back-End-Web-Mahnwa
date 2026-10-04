# Worker scan-import (FastAPI). Hanya dependensi runtime scan-import; kode legacy (Playwright/img2pdf) TIDAK disertakan.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/worker

# WITH_FIXTURE=1 menambah Pillow agar `python -m tools.fixture_source` (sumber demo orisinal) bisa dijalankan di image ini.
ARG WITH_FIXTURE=0
RUN pip install "fastapi>=0.110" "uvicorn[standard]>=0.28" "httpx>=0.27" "pydantic>=2" "sqlalchemy>=2" \
    && if [ "$WITH_FIXTURE" = "1" ]; then pip install "pillow"; fi

COPY app ./app
COPY tools ./tools

RUN useradd --system --uid 10001 --create-home worker && mkdir -p /srv/worker/staging /srv/worker/.fixture-data \
    && chown -R worker /srv/worker
USER worker

# Di dalam container worker harus bind ke 0.0.0.0 agar dapat dijangkau FE lewat jaringan compose.
# JANGAN publish port ini ke jaringan publik; batasi ke 127.0.0.1 / jaringan privat. WORKER_TOKEN wajib (kecuali SCAN_DEV_MODE=true).
ENV WORKER_BIND=0.0.0.0 WORKER_PORT=8000 STAGING_DIR=/srv/worker/staging
EXPOSE 8000
VOLUME ["/srv/worker/staging"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "app"]
