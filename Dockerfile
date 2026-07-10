FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (psycopg2-binary ships wheels, so this stays minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 10001 butler \
    && chown -R butler:butler /app
USER butler

EXPOSE 8000

# Default command runs the web UI + scheduler + notifier in one process.
CMD ["python", "-m", "app.main"]
