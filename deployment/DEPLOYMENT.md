# DeployGuard — Deployment

The app ships as a **single service**: the FastAPI backend serves the built
React frontend as static files and exposes the API under `/api/*`.

## Layout

```
DeployGuard/
├── backend/server.py      # FastAPI app  (imported as backend.server:app)
├── frontend/              # React + Vite  (build output in frontend/dist/)
├── deployment/            # this folder — Dockerfile, railway.toml, .dockerignore
├── main.py                # unmodified LangGraph pipeline (project root)
└── requirements.txt
```

`backend/server.py` runs the pipeline (`main.py`) as a subprocess from the
project root, so **the app must be launched from the project root** — the
module path resolves the root as the parent of `backend/`.

## Environment variables

| Variable        | Required | Notes                                                        |
| --------------- | -------- | ------------------------------------------------------------ |
| `GROQ_API_KEY`  | yes      | Groq key. Forwarded to the pipeline as `API_KEY`.            |
| `PORT`          | no       | Set by the platform; the start command binds to it.          |

## Run locally

```bash
# 1. build the frontend (Node 18+)
cd frontend && npm install && npm run build && cd ..

# 2. install backend deps
pip install -r requirements.txt

# 3. start from the PROJECT ROOT
GROQ_API_KEY=sk-... uvicorn backend.server:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — the SPA and the API are served from the same origin.

For frontend hot-reload during development, run `npm run dev` in `frontend/`
(Vite proxies `/api` to `http://localhost:8000`).

## Docker

The `Dockerfile` is multi-stage (build the frontend, then a Python runtime that
installs `git`, installs deps, and serves everything). **The build context must
be the project root**, so build with `-f`:

```bash
docker build -f deployment/Dockerfile -t deployguard .
docker run -p 8000:8000 -e GROQ_API_KEY=sk-... deployguard
```

> **`.dockerignore` location:** Docker only auto-loads `.dockerignore` from the
> **build-context root**. This repo keeps it in `deployment/`, so to have it
> take effect either (a) copy `deployment/.dockerignore` to the project root, or
> (b) with BuildKit, name it `deployment/Dockerfile.dockerignore` (BuildKit
> loads `<dockerfile>.dockerignore` automatically). Without this the build still
> works — it just copies more into the image than necessary.

## Railway (preferred)

`railway.toml` uses the Docker builder and points at `deployment/Dockerfile`:

```toml
[build]
builder = "dockerfile"
dockerfilePath = "deployment/Dockerfile"

[deploy]
startCommand = "uvicorn backend.server:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/api/health"
```

> **`railway.toml` location:** Railway looks for `railway.toml` at the **repo
> root** by default. Since it lives in `deployment/`, set the service's
> *Config-as-code path* to `deployment/railway.toml` in the Railway dashboard,
> or copy this file to the repo root.

Steps:
1. Push the repo to GitHub and create a Railway project from it.
2. Point the config path at `deployment/railway.toml` (see note above).
3. Set `GROQ_API_KEY` in the service **Variables**.
4. Deploy. Railway health-checks `GET /api/health`.
