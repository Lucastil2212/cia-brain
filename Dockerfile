FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY cia_brain /app/cia_brain
COPY ui /app/ui
COPY dags /app/dags
COPY tests /app/tests
RUN pip install --upgrade pip && pip install .
COPY scripts /app/scripts

RUN mkdir -p /data/raw /data/manifests /data/normalized /data/parquet /data/state /data/models

ENTRYPOINT ["python", "-m"]
CMD ["cia_brain.api"]
