FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NATS_SERVER_BIN=/usr/local/bin/nats-server

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl poppler-utils tesseract-ocr \
    && curl -fsSL https://github.com/nats-io/nats-server/releases/download/v2.11.3/nats-server-v2.11.3-linux-amd64.tar.gz \
      | tar -xz -C /tmp \
    && mv /tmp/nats-server-v2.11.3-linux-amd64/nats-server /usr/local/bin/nats-server \
    && chmod +x /usr/local/bin/nats-server \
    && rm -rf /tmp/nats-server-v2.11.3-linux-amd64 /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY cia_brain /app/cia_brain
COPY ui /app/ui
COPY dags /app/dags
COPY tests /app/tests
COPY scripts /app/scripts
RUN pip install --upgrade pip && pip install ".[ner]" \
    && python -m spacy download en_core_web_sm

RUN mkdir -p /data/raw /data/manifests /data/normalized /data/parquet /data/state /data/models /data/nats \
    && chmod +x /app/scripts/start-render.sh

# Empty entrypoint so Compose and Render can choose start commands freely.
ENTRYPOINT []
CMD ["python", "-m", "cia_brain.api"]
