# syntax=docker/dockerfile:1

ARG UV_VERSION=0.13.4

# ---------------------------------------------------------------------------
# Build stage: resolve and install the locked dependencies with uv.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# The lock file and the project metadata come first so that the dependency
# layer is cached independently of the source.
COPY pyproject.toml uv.lock README.md .python-version ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable


# ---------------------------------------------------------------------------
# Runtime stage.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    SYNC_MAPPING_FILE=/app/config/mapping/event.yaml \
    SYNC_STATE_PATH=/data/state.db

RUN groupadd --system --gid 10001 opsramp \
    && useradd --system --uid 10001 --gid opsramp --home-dir /app --shell /usr/sbin/nologin opsramp

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The mapping files are runtime data, not package data, so they are shipped
# separately from the wheel.
COPY config ./config

RUN mkdir -p /data && chown -R opsramp:opsramp /app /data

USER opsramp

VOLUME ["/data"]

ENTRYPOINT ["opsramp-gw"]
CMD ["--help"]
