# PlainBoard app: FastAPI backend + built React frontend. Build context: repository root.
FROM node:22-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.15-slim-bookworm AS backend
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY protocol /src/protocol
COPY app/pyproject.toml /src/app/pyproject.toml
COPY app/plainboard /src/app/plainboard
RUN pip install --no-cache-dir /src/protocol "/src/app[dev]"
COPY app/tests /src/app/tests

FROM backend AS final
COPY --from=frontend /frontend/dist /app/static
# The commit this image is built from, so the app can say when a newer one exists (services/version.py).
# Empty when the build context has no git data: then there is no notice. -I: the script's own folder is not
# on the import path, so a module next to it (secrets.py, settings.py) can never stand in for Python's own.
RUN --mount=type=bind,target=/ctx python -I /src/app/plainboard/services/version.py /ctx/.git > /app/commit
RUN useradd --create-home --uid 1000 plainboard && mkdir -p /data /uploads && chown plainboard:plainboard /data /uploads
USER plainboard
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"
# No WebSocket compression: the largest messages are pictures of the browser window, compressed already.
CMD ["uvicorn", "plainboard.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--ws-per-message-deflate", "false"]
