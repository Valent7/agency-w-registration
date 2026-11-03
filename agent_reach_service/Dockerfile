FROM node:22-bookworm-slim AS node

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

COPY --from=node /usr/local /usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv \
    && uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip \
    && agent-reach version \
    && agent-reach channels > /tmp/agent-reach-channels.json

WORKDIR /app
COPY agent_reach_service/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY agent_reach_service /app/agent_reach_service

CMD ["sh", "-c", "streamlit run /app/agent_reach_service/app.py --server.address 0.0.0.0 --server.port ${PORT:-10000} --server.headless true"]
