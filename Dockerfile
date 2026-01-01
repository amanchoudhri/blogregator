# Single-stage build using Playwright's base image
FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install system dependencies (build tools for psycopg2 and runtime deps)
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libpq-dev \
    postgresql-client \
    libpq5 \
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

# Set Playwright browser path to app directory (avoids lock issues with /ms-playwright)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers

# Install Playwright Chromium browser
RUN mkdir -p /app/.playwright-browsers && \
    .venv/bin/python -m playwright install chromium

# The Playwright image comes with user 'pwuser' (UID 1000)
# Create log directory and set permissions
RUN mkdir -p /app/logs && \
    chown -R pwuser:pwuser /app

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"

# Switch to non-root user (pwuser comes with Playwright image)
USER pwuser

# Ensure log directory exists with correct permissions
RUN mkdir -p /app/logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expose port
EXPOSE 8000

# Run server
CMD ["python", "-m", "blogregator.server"]
