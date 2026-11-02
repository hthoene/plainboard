# PlainBoard app: FastAPI backend + built React frontend. Build context: repository root.
FROM node:25-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS backend
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY protocol /src/protocol
COPY app/pyproject.toml /src/app/pyproject.toml
COPY app/plainboard /src/app/plainboard
RUN pip install --no-cache-dir /src/protocol "/src/app[dev]"
COPY app/tests /src/app/tests

FROM backend AS final
COPY --from=frontend /frontend/dist /app/static
RUN useradd --create-home --uid 1000 plainboard && mkdir -p /data /uploads && chown plainboard:plainboard /data /uploads
USER plainboard
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"
CMD ["uvicorn", "plainboard.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
