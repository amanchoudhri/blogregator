# Multi-stage build for optimized Docker image
FROM python:3.11-slim as builder

WORKDIR /app

# Install system dependencies (including build tools for psycopg2)
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libpq-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY sql ./sql

# Install Python dependencies
RUN uv sync --no-dev

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies (including libpq5 for psycopg2)
RUN apt-get update && apt-get install -y \
    postgresql-client \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/sql /app/sql
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

# Create non-root user
RUN useradd -m -u 1000 blogregator && \
    mkdir -p /app/logs && \
    chown -R blogregator:blogregator /app

USER blogregator

# Create log directory
RUN mkdir -p /app/logs

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expose port
EXPOSE 8000

# Run server
CMD ["python", "-m", "blogregator.server"]
