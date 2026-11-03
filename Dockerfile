# WhereAreMySats - generic Linux Docker image
# Build: docker compose build
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

# Install runtime dependencies only.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Create non-root user and data volume path.
RUN groupadd --gid 10001 lineageuser \
    && useradd --create-home --uid 10001 --gid 10001 lineageuser \
    && mkdir -p /app/data /app/backend /app/frontend \
    && chown -R lineageuser:lineageuser /app/data \
    && chmod 700 /app/data

# Copy application sources.
COPY backend /app/backend
COPY frontend /app/frontend

USER lineageuser

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["python", "-m", "backend.main"]
