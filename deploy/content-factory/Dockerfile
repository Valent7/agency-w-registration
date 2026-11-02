FROM node:22-bookworm-slim AS node

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_HOME=/opt/codex-home \
    HYPERFRAMES_NO_UPDATE_CHECK=1 \
    HYPERFRAMES_AGENT=1 \
    PRODUCER_LOW_MEMORY_MODE=1

COPY --from=node /usr/local /usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Actual Brag runtime: Codex plugin + Hyperframes renderer.
RUN npm install -g @openai/codex@latest hyperframes \
    && mkdir -p "$CODEX_HOME" \
    && codex plugin marketplace add latent-spaces/brag \
    && codex plugin add brag@brag \
    && codex --version \
    && hyperframes --version \
    && ffmpeg -version | head -n 1

WORKDIR /app
COPY requirements_content_factory.txt ./
RUN pip install -r requirements_content_factory.txt
COPY . /app

CMD ["python", "content_factory_worker.py", "--interval", "60", "--max-jobs", "2"]
