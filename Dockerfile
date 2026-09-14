FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping ca-certificates \
        iproute2 traceroute net-tools \
        libcap2-bin \
        tesseract-ocr tesseract-ocr-ind tesseract-ocr-eng \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && PING_BIN="$(command -v ping)" && setcap cap_net_raw+ep "$PING_BIN"

WORKDIR /app

COPY pyproject.toml ./
COPY data ./data
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir --no-cache-dir .

RUN useradd --create-home --shell /usr/sbin/nologin kela \
    && chown -R kela:kela /app \
    && mkdir -p /app/storage/documents /app/storage/reports /app/storage/images \
        /app/storage/exports /app/storage/temp \
    && chown -R kela:kela /app/storage

USER kela

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]