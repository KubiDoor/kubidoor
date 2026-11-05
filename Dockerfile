# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install . \
 && /opt/venv/bin/pip install "psycopg[binary]>=3.2,<4"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="kubidoor" \
      org.opencontainers.image.description="Lightweight Kubernetes health monitoring, incident detection, root cause analysis and safe automated recovery"
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KUBIDOOR_HOST=0.0.0.0 \
    KUBIDOOR_PORT=8000 \
    KUBIDOOR_DATABASE_URL=sqlite:////data/kubidoor.db \
    KUBIDOOR_MIGRATIONS_DIR=/app/migrations
RUN groupadd --system --gid 10001 kubidoor \
 && useradd --system --uid 10001 --gid kubidoor --home-dir /app --shell /usr/sbin/nologin kubidoor \
 && mkdir -p /data /app && chown kubidoor:kubidoor /data
COPY --from=builder /opt/venv /opt/venv
COPY --chown=kubidoor:kubidoor migrations /app/migrations
WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"
ENTRYPOINT ["kubidoor"]
