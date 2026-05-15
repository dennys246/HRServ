# syntax=docker/dockerfile:1.7

# HRServ runtime image. Two-stage build: install with uv into a venv in stage 1,
# copy the venv + source into a slim final image.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv is fetched as a static binary so we don't pull a Rust toolchain.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/

WORKDIR /app

# Resolve dependencies before copying the source. Better layer caching: lock
# changes invalidate the dep layer; source-only edits don't.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

COPY hrserv ./hrserv
COPY migrations ./migrations
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Add a non-root user. The uvicorn process should not run as root.
RUN groupadd --system --gid 1000 hrserv \
    && useradd --system --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin hrserv

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/hrserv /app/hrserv
COPY --from=builder /app/migrations /app/migrations

WORKDIR /app
USER hrserv

EXPOSE 8000

# Healthcheck the container itself by hitting /healthz over loopback.
# `--start-period=180s` covers the full create_pool_with_retry budget
# (1+2+3+5+8+13+21+30+30+30 = 143s) plus slack. Without this margin, the
# container can be marked unhealthy mid-retry-loop (~60s into boot) even
# though the lifespan is still legitimately trying to come up. Was 10s
# originally; bumped to 60s, then 180s after seeing the retry tail exceed
# the prior margin under bridge-flap scenarios on jib-jab.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)" \
    || exit 1

# --workers=1: at HRF-upload volume there's zero throughput need for >1
# worker, and uvicorn's multi-worker master has known partial-startup edge
# cases (one worker's lifespan succeeds while another's fails). A single
# worker makes startup deterministic. See docs/FOLLOWUPS.md for the
# "when to re-enable multi-worker" trigger.
CMD ["uvicorn", "hrserv.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
