# Ouroboros — Docker image for web UI runtime
# Usage:
#   docker build -t ouroboros-web .
#   docker run --rm -p 8765:8765 ouroboros-web

FROM python:3.10-slim

# Copy uv binary from official image (no pip bootstrap needed)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Working directory
ENV APP_HOME=/app
WORKDIR ${APP_HOME}

# Install Python dependencies via uv (deterministic from lockfile)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application
COPY . .

# Default environment
ENV OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}

EXPOSE 8765

ENTRYPOINT ["uv", "run", "python", "server.py"]
