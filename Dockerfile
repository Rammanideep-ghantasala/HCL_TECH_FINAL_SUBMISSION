# syntax=docker/dockerfile:1
#
# One image: the built React frontend, the FastAPI backend, the ticket data,
# the embedding model and the precomputed embeddings. Nothing is downloaded
# when the container starts. Retrieval needs no credentials; optional summaries
# require OPENAI_API_KEY at runtime and outbound HTTPS access to the provider.
#
#   docker build -t ticket-similarity-assistant .
#   docker run --rm -p 8080:8080 ticket-similarity-assistant

ARG NODE_IMAGE=node:24-alpine
ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8

# ---------------------------------------------------------------- frontend
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- uv binary
FROM ${UV_IMAGE} AS uv

# ------------------------------------------- backend: deps, model, embeddings
FROM ${PYTHON_IMAGE} AS backend
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/backend

# Dependencies first, from the lock file, so this layer is reused across code changes.
# PyTorch comes from the CPU-only index configured in pyproject.toml (no CUDA libraries).
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY backend/config ./config
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY starter_kit/data/tickets.json starter_kit/data/new_tickets_example.json /app/data/

ENV PATH=/app/backend/.venv/bin:$PATH \
    TICKETS_PATH=/app/data/tickets.json \
    EXAMPLES_PATH=/app/data/new_tickets_example.json \
    MODEL_DIR=/app/models/all-MiniLM-L6-v2 \
    ARTIFACTS_DIR=/app/artifacts

# The only network access for the model: a pinned revision, fetched at build time.
RUN python -m app.prepare fetch-model \
    && rm -rf /app/models/all-MiniLM-L6-v2/.cache
# Precompute the historical embeddings offline, proving the model loads without the network.
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m app.prepare build-index \
    && python -m compileall -q app

# ----------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/state \
    && chown 10001:10001 /app/state

COPY --from=backend /app/backend/.venv /app/backend/.venv
COPY --from=backend /app/backend/app /app/backend/app
COPY --from=backend /app/backend/config /app/backend/config
COPY --from=backend /app/backend/alembic.ini /app/backend/alembic.ini
COPY --from=backend /app/backend/migrations /app/backend/migrations
COPY --from=backend /app/data /app/data
COPY --from=backend /app/models /app/models
COPY --from=backend /app/artifacts /app/artifacts
COPY --from=frontend /build/frontend/dist /app/frontend/dist

ENV PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    TOKENIZERS_PARALLELISM=false \
    TQDM_DISABLE=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    TRANSFORMERS_VERBOSITY=error \
    HOST=0.0.0.0 \
    PORT=8080 \
    TICKETS_PATH=/app/data/tickets.json \
    EXAMPLES_PATH=/app/data/new_tickets_example.json \
    MODEL_DIR=/app/models/all-MiniLM-L6-v2 \
    ARTIFACTS_DIR=/app/artifacts \
    FRONTEND_DIST=/app/frontend/dist \
    DATA_DIR=/app/state \
    TICKET_STORE=json

WORKDIR /app/backend
USER 10001:10001
EXPOSE 8080
# Saved tickets (JSON mode) and the embedding cache. Mount a volume to keep them.
VOLUME ["/app/state"]

# Liveness only. Readiness (GET /ready) is what a load balancer should check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "app"]
