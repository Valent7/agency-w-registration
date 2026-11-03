FROM node:22-bookworm-slim AS node

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_HOME_BASE=/opt/codex-home \
    MALLOC_ARENA_MAX=2 \
    NODE_OPTIONS=--max-old-space-size=192

COPY --from=node /usr/local /usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates git chromium fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex@latest \
    && mkdir -p "$CODEX_HOME_BASE" \
    && CODEX_HOME="$CODEX_HOME_BASE" codex plugin marketplace add latent-spaces/brag \
    && CODEX_HOME="$CODEX_HOME_BASE" codex plugin add brag@brag \
    && codex --version \
    && ffmpeg -version | head -n 1 \
    && chromium --version \
    && mv /usr/bin/chromium /usr/bin/chromium-real \
    && printf '%s\n' '#!/bin/sh' 'exec /usr/bin/chromium-real --headless --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-extensions --disable-background-networking --disable-sync --no-first-run --renderer-process-limit=1 "$@"' > /usr/local/bin/chromium \
    && chmod +x /usr/local/bin/chromium

WORKDIR /app
COPY brag_service/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY brag_service /app/brag_service

CMD ["sh", "-c", "streamlit run /app/brag_service/app.py --server.address 0.0.0.0 --server.port ${PORT:-10000} --server.headless true"]
