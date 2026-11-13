# rbit — Next-Gen Full-Screen Terminal AI Coding Agent & CLI
#
# Build:  docker build -t ghcr.io/dreyzedev/rbit:local .
# Run:    docker run -it --rm -v "$PWD:/workspace" -w /workspace ghcr.io/dreyzedev/rbit:local run "fix the failing test"
# TUI:    docker run -it --rm -v "$PWD:/workspace" -w /workspace ghcr.io/dreyzedev/rbit:local

FROM python:3.12-slim

LABEL org.opencontainers.image.title="rbit" \
      org.opencontainers.image.description="Next-Gen Full-Screen Terminal AI Coding Agent & CLI" \
      org.opencontainers.image.url="https://github.com/DreyzeDev/rbit" \
      org.opencontainers.image.source="https://github.com/DreyzeDev/rbit" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is needed for @diff/@git/@staged attaches, the git tool and /commit
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY rbit ./rbit

RUN pip install --no-cache-dir . && rm -rf /app

# Non-root user; sessions/config/checkpoints live in the mounted workspace's .rbit/
RUN useradd --create-home --shell /bin/bash rbit
USER rbit

WORKDIR /workspace

ENTRYPOINT ["rbit"]
CMD ["--help"]
