# RiskMesh API server
#   docker build -t riskmesh .
#   docker run -p 8000:8000 -e RISKMESH_BACKEND=sql -e RISKMESH_SQL_URL=... riskmesh
# See docker-compose.yml for a complete setup with PostgreSQL and backups.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RISKMESH_CONFIG=/config/riskmesh.toml

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY riskmesh ./riskmesh
RUN pip install ".[api,sql,neo4j,postgres]" \
    && useradd --system --uid 1000 --home /data riskmesh \
    && mkdir -p /data /config && chown riskmesh /data

USER riskmesh
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["riskmesh", "serve", "--host", "0.0.0.0", "--port", "8000"]
