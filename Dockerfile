# --- Stage 1: Builder ---
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Final Image ---
FROM python:3.11-slim-bookworm AS final

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /home/appuser/app

# Copy venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Need these ENVs in final stage too for runtime
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Copy app code
COPY . .

# Set ownership
RUN chown -R appuser:appuser /home/appuser/app

USER appuser

# Healthcheck: Runs every 60s. If it fails 3 times (3 minutes), Docker marks container as "unhealthy"
# (You can use autoheal containers to restart it automatically)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "main.py", "--log"]