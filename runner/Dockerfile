# PlainBoard runner: the environment agents work in. Agents run as user "agent" with
# passwordless sudo, so they have full control over this container (not over the app).
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg sudo build-essential ripgrep jq less nano \
        openssh-client unzip zip procps tzdata \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Node.js (for JS projects and npx-based MCP servers) and uv (for Python / uvx MCP servers)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Codex CLI for the Codex provider. Pinned: PlainBoard talks to its app server protocol.
ARG CODEX_VERSION=0.166.0
RUN npm install -g --no-audit --no-fund @openai/codex@${CODEX_VERSION} && codex --version

# Google Antigravity CLI for the Antigravity provider (ACP over stdio). Pinned with sha512 and
# installed read-only (0555) so the CLI auto-updater cannot replace it in place.
ARG ANTIGRAVITY_VERSION=1.5.15
ARG ANTIGRAVITY_BUILD=5514532022517760
ARG ANTIGRAVITY_SHA512_AMD64=af3f0be58b8475940d79f9abea19e762ecc3d265ea9f56cff8dd805e27ba299b44a305a79504a7ffe6ebdfa29a294c56edc4caed1875990bfaf2fef1508ec8b0
ARG ANTIGRAVITY_SHA512_ARM64=d64ad759781b50dfacfa7d9826c0ad469959d01bdf5a170acd4c83472eba31c677baf0052ac8ab48eef40f0d3f0e26057e752aabd24331c79f4c881d4b872125
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) folder="linux-x64"; file="cli_linux_x64.tar.gz"; sha="$ANTIGRAVITY_SHA512_AMD64" ;; \
        arm64) folder="linux-arm"; file="cli_linux_arm64.tar.gz"; sha="$ANTIGRAVITY_SHA512_ARM64" ;; \
        *) echo "Unsupported architecture for Antigravity CLI: $arch" >&2; exit 1 ;; \
    esac \
    && tmp="$(mktemp -d)" \
    && curl -fsSL "https://storage.googleapis.com/antigravity-public/antigravity-cli/${ANTIGRAVITY_VERSION}-${ANTIGRAVITY_BUILD}/${folder}/${file}" -o "$tmp/agy.tar.gz" \
    && echo "${sha}  $tmp/agy.tar.gz" | sha512sum -c - \
    && tar -xzf "$tmp/agy.tar.gz" -C "$tmp" \
    && install -m 0555 "$tmp/antigravity" /usr/local/bin/antigravity \
    && ln -sf /usr/local/bin/antigravity /usr/local/bin/agy \
    && rm -rf "$tmp"

# Chromium for the browser window: pages of the agents' servers shown in PlainBoard as they are at
# localhost (plainboard_runner/browser.py). About 700 MB; build with PLAINBOARD_BROWSER=0 to leave it out.
# DejaVu gives the generic families (sans-serif, system-ui) a font: without it everything is monospace.
ARG PLAINBOARD_BROWSER=1
RUN if [ "$PLAINBOARD_BROWSER" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Git talks to GitHub through the gh credential helper (uses GH_TOKEN from each run).
RUN git config --system credential.https://github.com.helper '!gh auth git-credential' \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory '*'

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent \
    && mkdir -p /workspaces /uploads && chown agent:agent /workspaces /uploads

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="/home/agent/.local/bin:/home/agent/.npm-global/bin:$VIRTUAL_ENV/bin:$PATH" \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global

# Agents run non-interactive shells, which skip ~/.profile. Load it anyway, so whatever an installer
# adds there works for every agent, whatever the toolchain.
RUN printf '%s\n' 'if [ -z "$PLAINBOARD_ENV_LOADING" ] && [ -f "$HOME/.profile" ]; then export PLAINBOARD_ENV_LOADING=1; . "$HOME/.profile"; unset PLAINBOARD_ENV_LOADING; fi' > /etc/plainboard-env.sh
ENV BASH_ENV=/etc/plainboard-env.sh

COPY protocol /src/protocol
COPY runner/pyproject.toml /src/runner/pyproject.toml
COPY runner/plainboard_runner /src/runner/plainboard_runner
# PlainBoard's own code is not in the venv but in /opt/plainboard/current (plainboard.pth), so that an
# external runner can update it by itself (plainboard_runner/selfupdate.py, launcher.py).
RUN pip install --no-cache-dir /src/protocol "/src/runner[dev]" \
    && pip uninstall -y plainboard-runner plainboard-protocol \
    && mkdir -p /opt/plainboard/image \
    && cp -r /src/runner/plainboard_runner /src/protocol/plainboard_protocol /opt/plainboard/image/ \
    && ln -s image /opt/plainboard/current \
    && echo /opt/plainboard/current > $VIRTUAL_ENV/lib/python3.12/site-packages/plainboard.pth \
    && chown -R agent:agent /opt/plainboard \
    && ln -s $VIRTUAL_ENV/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude
COPY runner/tests /src/runner/tests
# The commit this image is built from: an external runner tells PlainBoard, which shows a mismatch.
RUN --mount=type=bind,target=/ctx python -I /ctx/app/plainboard/services/version.py /ctx/.git > /etc/plainboard-commit

# A self-update compares these with the new version's: when they differ, it needs a new image.
COPY runner/Dockerfile runner/launcher.py /usr/local/lib/plainboard/

USER agent
WORKDIR /workspaces
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"
# No WebSocket compression: its only socket carries pictures of the browser window, compressed already.
CMD ["python", "-I", "/usr/local/lib/plainboard/launcher.py", "uvicorn", "plainboard_runner.main:app", "--host", "0.0.0.0", "--port", "8090", "--ws-per-message-deflate", "false", "--timeout-keep-alive", "75"]
