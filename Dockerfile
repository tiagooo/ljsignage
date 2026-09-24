# lj-signage: uma imagem para os dois serviços (web e worker). Ver docs/SPEC.md §11.
FROM python:3.12-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_APP=lj_signage \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    DEVICES_FILE=/app/config/devices.yaml \
    TZ=Europe/Lisbon

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 lj \
    && mkdir -p /data \
    && chown lj:lj /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY lj_signage ./lj_signage
COPY migrations ./migrations
COPY config/devices.example.yaml ./config/devices.example.yaml
COPY docker ./docker


# --- testes (docker build --target test .) -------------------------------------------------
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY docs ./docs
USER lj
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# --- imagem final ---------------------------------------------------------------------------
FROM base AS app
USER lj
VOLUME ["/data"]
EXPOSE 8080
CMD ["/app/docker/web-start.sh"]
