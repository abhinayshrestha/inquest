# The Inquest backend (API, runner, private MCP listener). Also runs the one-off
# provisioning and migration jobs with a different command and environment
# (docs/deployment.md). Build from the repository root:
#   docker build -f deploy/app.Dockerfile -t inquest-app .
# Pin both base images by digest for production builds.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
# Dependencies first (cached across source changes); exactly the lockfile, no dev tools.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin inquest
USER inquest
# Configuration and secrets come from the environment at run time, never from the image.
CMD ["python", "-m", "src.serve"]
