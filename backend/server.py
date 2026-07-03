"""
server.py — DeployGuard web backend.

Wraps the EXISTING LangGraph pipeline (main.py) without modifying it.

Why a subprocess instead of `from main import graph`?
--------------------------------------------------------
main.py runs the whole pipeline at *module import time* (it calls
`graph.invoke(...)` at the bottom of the file) and the LLM-reasoning node
blocks on `input("> ")` to collect deployment-context answers from stdin.
Neither is compatible with importing the graph into a long-lived web server.

So we run `python main.py` as a child process:
  * the repo URL is handed to it via the REP_URL env var (main.py reads
    GIT_URL = os.getenv("REP_URL"))
  * the Groq key is handed to it via API_KEY (main.py reads
    GROQ_API_KEY = os.getenv("API_KEY"))
  * we stream its stdout to the browser as Server-Sent Events
  * when it prints its questions block and blocks on input(), we surface the
    questions to the UI, buffer the user's answers, and write them (comma
    separated, as main.py expects) to the child's stdin to unblock it
  * when it exits 0, reliability_report.md has been written to disk and we
    emit a `complete` event.

The pipeline shares mutable on-disk state (build/, reliability_report.md,
<repo>_clone/), so only one analysis may run at a time — enforced with a lock.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

load_dotenv()

# Absolute path to the PROJECT ROOT. server.py lives in backend/, but the
# unmodified pipeline (main.py), prompts/, build/ and the generated report all
# live one directory up — so we resolve the root as the parent of backend/.
# (Do NOT rely on CWD; the parent is derived from this file's location.)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(ROOT_DIR, "reliability_report.md")
FRONTEND_DIST = os.path.join(ROOT_DIR, "frontend", "dist")

# The Groq key. Prefer the conventional GROQ_API_KEY; fall back to the
# pipeline's own API_KEY so nothing breaks if only that is set.
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("API_KEY")

# The pipeline entry point we spawn. Overridable only to ease testing; in
# production this is always the real, unmodified main.py.
PIPELINE_SCRIPT = os.getenv("PIPELINE_SCRIPT", "main.py")

# main.py prints this line right before it invokes the graph — our signal
# that the child is alive and the pipeline is starting.
_WORKFLOW_MARKER = "Workflow saved as workflow.png"
_QUESTIONS_HEADER = "Questions:"
_ANSWERS_MARKER = "separating them with a comma"
_QUESTION_LINE = re.compile(r"^\s*\d+[.)]\s+(.*\S)\s*$")

# Only one pipeline run at a time (shared build/ + report on disk).
_run_lock = asyncio.Lock()


class Job:
    """One analysis run: its child process, its outbound SSE queue, and the
    interactive Q&A bookkeeping."""

    def __init__(self, job_id: str, repo_url: str):
        self.id = job_id
        self.repo_url = repo_url
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self.questions: List[str] = []
        self.answers: List[str] = []
        self.answers_ready = asyncio.Event()
        self.awaiting_answers = False
        self.finished = False


# Active jobs, keyed by id, so /api/answer can reach a running child's stdin.
JOBS: Dict[str, Job] = {}


async def _emit(job: Job, ev_type: str, content) -> None:
    """Push one SSE event onto the job's queue."""
    await job.queue.put({"type": ev_type, "content": content})


async def _run_pipeline(job: Job) -> None:
    """Spawn `python main.py`, stream its output, drive the Q&A handshake,
    and translate everything into SSE events on job.queue."""
    async with _run_lock:
        try:
            await _emit(job, "stage", "Ingesting repository...")

            env = os.environ.copy()
            env["REP_URL"] = job.repo_url
            if GROQ_API_KEY:
                env["API_KEY"] = GROQ_API_KEY
            # Force unbuffered child stdout so we see prints as they happen.
            env["PYTHONUNBUFFERED"] = "1"

            job.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", PIPELINE_SCRIPT,
                cwd=ROOT_DIR,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,  # report markdown can be large; raise line cap
            )

            await _pump_stdout(job)

            return_code = await job.proc.wait()

            if job.finished:
                return  # an error was already emitted

            if return_code == 0 and os.path.exists(REPORT_PATH):
                await _emit(job, "stage", "Report ready.")
                await _emit(job, "complete", "Analysis complete. Report generated.")
            elif return_code == 0:
                await _emit(job, "error",
                            "Pipeline finished but no report file was found.")
            else:
                await _emit(job, "error",
                            f"Pipeline exited with code {return_code}. "
                            f"Check the server logs for details.")
        except Exception as exc:  # noqa: BLE001 — surface anything to the client
            await _emit(job, "error", f"{type(exc).__name__}: {exc}")
        finally:
            job.finished = True
            await job.queue.put(None)  # sentinel: closes the SSE stream


async def _pump_stdout(job: Job) -> None:
    """Read the child's stdout line by line and turn it into SSE events.

    State machine for the interactive questions:
      NORMAL      -> stream lines as `message`
      (see "Questions:")        -> start collecting numbered questions
      (see comma marker)        -> emit `question` events, then WAIT for the
                                   user's answers, write them to stdin, resume
    """
    proc = job.proc
    assert proc is not None and proc.stdout is not None

    started = False
    triggered = False  # have we already handed the questions to the user?
    in_block = False   # inside the questions block (suppress its prose lines)

    while True:
        try:
            raw = await proc.stdout.readline()
        except (asyncio.LimitOverrunError, ValueError):
            # Line longer than the buffer limit — drain what we can and move on.
            raw = await proc.stdout.read(1024 * 1024)
        if not raw:
            break  # EOF

        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        stripped = line.strip()

        if not started and _WORKFLOW_MARKER in line:
            started = True
            await _emit(job, "stage", "Analyzing repository & building call graph...")
            continue

        # ---- questions block ----------------------------------------------
        # The "Questions:" banner marks a fresh block: reset any captured lines.
        if _QUESTIONS_HEADER in stripped and not triggered:
            job.questions = []
            in_block = True
            await _emit(job, "stage", "LLM reasoning — a few deployment questions...")
            continue

        # The "...separating them with a comma" line is the last prose line
        # before main.py's input("> ") prompt. Trigger the Q&A here. This is
        # decoupled from the banner above so a missed/altered banner can't leave
        # the child blocked on input() forever.
        if _ANSWERS_MARKER in stripped and not triggered:
            triggered = True
            in_block = False
            await _wait_for_answers(job)
            continue

        # Any "1. ...", "2) ..." line before the trigger is a question. We
        # capture these unconditionally (not just after the banner) so the
        # questions surface even if the banner line was not recognised; the
        # first one also opens the block so we can suppress its prose lines.
        if not triggered:
            m = _QUESTION_LINE.match(line)
            if m:
                job.questions.append(m.group(1))
                in_block = True
                continue
            if in_block:
                # prose inside the block (e.g. "Provide answers in the same
                # order.") — don't surface it as a chat message.
                continue

        # ---- normal output -------------------------------------------------
        # Skip the leftover input prompt echo ("> ") and empty lines.
        if stripped in ("", ">"):
            continue
        if stripped.startswith("> "):
            stripped = stripped[2:].strip()
            if not stripped:
                continue
            line = stripped

        await _emit(job, "message", line)


async def _wait_for_answers(job: Job) -> None:
    """Surface the collected questions to the UI, wait until the user has
    answered all of them, then write the comma-joined line to the child's
    stdin (exactly what main.py's `input()` + split(',') expects)."""
    if not job.questions:
        # Defensive: no questions parsed — send a blank line so input() returns.
        await _write_stdin(job, b"\n")
        return

    job.awaiting_answers = True
    await _emit(job, "questions", job.questions)

    await job.answers_ready.wait()
    job.awaiting_answers = False

    if job.finished:  # client disconnected / job was torn down while waiting
        return

    # main.py: answers = [a.strip() for a in raw_answers.split(",")]
    payload = ",".join(a.replace(",", " ") for a in job.answers) + "\n"
    if await _write_stdin(job, payload.encode("utf-8")):
        await _emit(job, "stage", "Assessing reliability & generating report...")


async def _write_stdin(job: Job, data: bytes) -> bool:
    """Write to the child's stdin, tolerating a process that has already died.
    Returns True on success."""
    try:
        job.proc.stdin.write(data)
        await job.proc.stdin.drain()
        return True
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        return False


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="DeployGuard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/analyse")
async def analyse(request: Request):
    body = await request.json()
    repo_url = (body or {}).get("repo_url", "").strip()
    if not repo_url:
        return JSONResponse({"error": "repo_url is required"}, status_code=400)

    job_id = uuid.uuid4().hex
    job = Job(job_id, repo_url)
    JOBS[job_id] = job

    # Kick off the pipeline in the background; the SSE generator just drains
    # the queue, so the run is not tied to this single HTTP request lifetime.
    asyncio.create_task(_run_pipeline(job))

    async def event_generator():
        # First event always carries the job id so the client can post answers.
        yield {"data": json.dumps({"type": "job", "content": job_id})}
        try:
            while True:
                # Wait for the next event, but wake periodically to notice a
                # client that has navigated away.
                try:
                    item = await asyncio.wait_for(job.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    continue
                if item is None:
                    break
                yield {"data": json.dumps(item)}
        finally:
            JOBS.pop(job_id, None)
            # If the client left before the run finished, don't leave a child
            # blocked on input() holding the global run lock forever.
            if not job.finished and job.proc and job.proc.returncode is None:
                job.finished = True
                # Unblock the pump if it is waiting on answers, then kill.
                job.answers_ready.set()
                try:
                    job.proc.kill()
                except ProcessLookupError:
                    pass

    # sep="\n" so events are separated by "\n\n" (not the default "\r\n\r\n"),
    # matching the frontend's SSE frame parser.
    return EventSourceResponse(event_generator(), sep="\n")


@app.post("/api/answer")
async def answer(request: Request):
    """Receive one answer at a time. Buffer it; once every question has an
    answer, flush them to the running child's stdin to unblock the pipeline."""
    body = await request.json()
    job_id = (body or {}).get("job_id", "")
    ans = (body or {}).get("answer", "")

    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown or finished job"}, status_code=404)
    if not job.awaiting_answers:
        return JSONResponse({"error": "job is not waiting for answers"},
                            status_code=409)

    job.answers.append(ans if isinstance(ans, str) else str(ans))
    remaining = len(job.questions) - len(job.answers)

    if remaining <= 0:
        job.answers_ready.set()
        return {"status": "received", "remaining": 0}

    return {"status": "received", "remaining": remaining}


@app.get("/api/report")
async def report():
    if not os.path.exists(REPORT_PATH):
        return JSONResponse(
            {"error": "No report has been generated yet. Run an analysis first."},
            status_code=404,
        )
    return FileResponse(
        REPORT_PATH,
        media_type="text/markdown",
        filename="deployguard-report.md",
        headers={
            "Content-Disposition": 'attachment; filename="deployguard-report.md"'
        },
    )


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api/* wins). Serves the built React SPA
# and falls back to index.html for client-side routes.
# ---------------------------------------------------------------------------
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    # For non-API GET routes, hand back index.html so the SPA can route.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    index = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({"error": "frontend not built"}, status_code=404)
