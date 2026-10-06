# syntax=docker/dockerfile:1
FROM python:3.12-slim

# libgl1/libglib2.0-0 are needed by opencv-headless when RapidOCR lands (Phase 4).
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Render overrides this per service via dockerCommand. Shell form so $PORT expands;
# exec so SIGTERM reaches the process.
CMD ["sh", "-c", "exec uvicorn tokensage.api.app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
