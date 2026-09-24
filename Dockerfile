FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NATS_SERVER_BIN=/usr/local/bin/nats-server

ARG NATS_VERSION=2.11.3
ARG NATS_SHA256=9cdab8b2e2488128caee6519e2f15f1aa33a78b4386ee1776a06b4818d7ec197

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl poppler-utils tesseract-ocr \
    && curl -fsSL "https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}/nats-server-v${NATS_VERSION}-linux-amd64.tar.gz" \
      -o /tmp/nats.tgz \
    && echo "${NATS_SHA256}  /tmp/nats.tgz" | sha256sum -c - \
    && tar -xz -C /tmp -f /tmp/nats.tgz \
    && mv "/tmp/nats-server-v${NATS_VERSION}-linux-amd64/nats-server" /usr/local/bin/nats-server \
    && chmod +x /usr/local/bin/nats-server \
    && rm -rf /tmp/nats-server-v${NATS_VERSION}-linux-amd64 /tmp/nats.tgz /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY cia_brain /app/cia_brain
COPY ui /app/ui
COPY dags /app/dags
COPY tests /app/tests
COPY scripts /app/scripts
RUN pip install --upgrade pip && pip install ".[ner]" \
    && python -m spacy download en_core_web_sm \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /data/raw /data/manifests /data/normalized /data/parquet /data/state /data/models /data/nats \
    && chown -R app:app /app /data \
    && chmod +x /app/scripts/start-render.sh

USER app

# Empty entrypoint so Compose and Render can choose start commands freely.
ENTRYPOINT []
CMD ["python", "-m", "cia_brain.api"]
