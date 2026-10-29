# syntax=docker/dockerfile:1
# One image for the API and the UI. The server serves web/dist when it exists.

# 1. Build the UI. Without web/src, there is no UI to build: only the API runs.
FROM node:22-slim AS web
WORKDIR /web
COPY web/ ./
RUN mkdir -p /out && if [ -d src ]; then \
      (if [ -f package-lock.json ]; then npm ci; else npm install; fi) && npm run build && cp -r dist /out/dist; \
    fi

# 2. The app. The Agent SDK has the Claude CLI as a native binary that needs glibc: do not use Alpine.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini README.md ./
COPY migrations/ ./migrations/
COPY job_finder/ ./job_finder/
RUN uv sync --frozen --no-dev
COPY --from=web /out/ ./web/
# `job-finder login` runs `claude setup-token` from PATH.
RUN ln -s /app/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["job-finder", "serve"]
