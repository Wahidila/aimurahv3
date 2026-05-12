# AIMurahV3 — Kiro proxy gateway
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIMURAH_HOME=/data

WORKDIR /app

# System deps: bcrypt wheels exist for slim; curl is only for HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY aimurah ./aimurah
COPY run.py ./run.py
COPY README.md ./README.md

RUN useradd --system --home /data --shell /usr/sbin/nologin aimurah \
    && mkdir -p /data \
    && chown -R aimurah:aimurah /data /app

USER aimurah

EXPOSE 7830 7831

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${AIMURAH_PROXY_PORT:-7830}/health" || exit 1

# Foreground run — systemd / docker will supervise.
CMD ["python", "-m", "aimurah", "start", "--foreground"]
