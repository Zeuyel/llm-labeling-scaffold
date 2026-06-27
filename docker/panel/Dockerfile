FROM node:22-slim AS frontend

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend /app/frontend/dist ./frontend/dist
RUN pip install -e ".[baseline,argilla,mlflow]"
ARG PANEL_UID=1000
ARG PANEL_GID=1000
RUN groupadd --gid "${PANEL_GID}" panel \
    && useradd --uid "${PANEL_UID}" --gid "${PANEL_GID}" --create-home --shell /usr/sbin/nologin panel \
    && mkdir -p /app/runs /app/tasks \
    && chown -R panel:panel /app

EXPOSE 8765

USER panel:panel
CMD ["python", "-m", "llm_labeling_scaffold.cli", "panel", "--host", "0.0.0.0", "--port", "8765", "--static-dir", "frontend/dist", "--tasks-root", "tasks", "--runs-root", "runs"]
