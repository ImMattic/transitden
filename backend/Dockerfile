FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# Run Alembic migrations then start Uvicorn.
# --limit-concurrency turns excess concurrent requests into fast 503s instead
# of letting them queue toward OOM on the small VM; legit concurrency is a
# handful of requests. UVICORN_WORKERS > 1 is safe for ingestion (one worker
# wins an advisory lock, see services/leader.py) but the rate limiter and /sim
# state are per-process, so leave it at 1 wherever /sim is enabled.
CMD ["sh", "-c", "cd /app && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1} --limit-concurrency 64 --timeout-keep-alive 5 --backlog 128"]
