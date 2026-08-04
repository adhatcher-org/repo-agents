# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS node_runtime
FROM ghcr.io/astral-sh/uv:0.11.7 AS uv_runtime

FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="repo-agent" \
      org.opencontainers.image.description="Foundation runtime for scheduled repository maintenance agents" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    AGENT_CONFIG=/config/repos.yml \
    AGENT_STATE_DIR=/data \
    AGENT_DEFINITIONS_DIR=/app/agents \
    OLLAMA_BASE_URL=http://ollama:11434

# git/gh provide repository and GitHub API access; curl/jq support deterministic
# probes and inspection. No Docker client/socket is included or required.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        bash \
        ca-certificates \
        curl \
        git \
        gh \
        jq \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Keep modern Node tooling available for repositories that define Node quality gates.
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node_runtime /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv_runtime /uv /uvx /bin/

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid agent --create-home --shell /bin/bash agent \
    && mkdir -p /app /data /config /projects /logs \
    && chown -R agent:agent /app /data /config /projects /logs

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY agents ./agents
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

USER agent
ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["repo-agent", "health"]
