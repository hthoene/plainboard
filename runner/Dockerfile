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

# Git talks to GitHub through the gh credential helper (uses GH_TOKEN from each run).
RUN git config --system credential.https://github.com.helper '!gh auth git-credential' \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory '*'

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent \
    && mkdir -p /workspaces /uploads && chown agent:agent /workspaces

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
RUN pip install --no-cache-dir /src/protocol "/src/runner[dev]" \
    && ln -s $VIRTUAL_ENV/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude
COPY runner/tests /src/runner/tests

USER agent
WORKDIR /workspaces
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"
CMD ["uvicorn", "plainboard_runner.main:app", "--host", "0.0.0.0", "--port", "8090"]
