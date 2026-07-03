# syntax=docker/dockerfile:1
#
# Build context MUST be the project root, not deployment/. Build with:
#   docker build -f deployment/Dockerfile -t deployguard .
# (All COPY paths below are relative to the project root.)

# ── Stage 1 — build the React frontend ──────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ── Stage 2 — Python runtime that serves API + built frontend ───────
FROM python:3.11-slim
WORKDIR /app

# git is required at runtime: the pipeline clones the target repo (GitPython).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

EXPOSE 8000
# server.py now lives in backend/ (imported as the backend.server module).
CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
