FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update --error-on=any \
    && apt-get install --yes --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements_content_factory.txt /app/requirements_content_factory.txt
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then \
        PIP_CERT=/run/secrets/build_ca python -m pip install -r /app/requirements_content_factory.txt; \
    else \
        python -m pip install -r /app/requirements_content_factory.txt; \
    fi
COPY . /app

CMD ["python", "content_factory_worker.py", "--interval", "60", "--max-jobs", "2"]
