import logging
import threading

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, llm, pipeline
from .config import DECLINE_REASONS, POSTED_WITHIN, ROOT, load_settings
from .resume_parser import extract_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Job Search Assistant")
db.init_db()

STATIC = ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- resumes

@app.post("/api/resumes")
async def upload_resume(file: UploadFile = File(...)):
    data = await file.read()
    try:
        text = extract_text(file.filename, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resume_id = db.add_resume(file.filename, text)
    return {"id": resume_id, "chars": len(text), "preview": text[:500]}


@app.get("/api/resumes")
def resumes():
    return db.list_resumes()


@app.post("/api/resumes/{resume_id}/activate")
def activate(resume_id: int):
    db.activate_resume(resume_id)
    return {"ok": True}


# ---------------------------------------------------------------- jobs

@app.get("/api/jobs")
def jobs():
    resume = db.active_resume()
    threshold = load_settings().get("llm", {}).get("match_threshold", 70)
    return {"threshold": threshold, "resume": resume and resume["filename"], "jobs": db.list_jobs(resume and resume["id"])}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return job


class Decision(BaseModel):
    status: str  # applied | not_interested | new
    reason: str | None = None
    note: str | None = None


@app.post("/api/jobs/{job_id}/decision")
def decide(job_id: int, d: Decision):
    if d.status not in ("applied", "not_interested", "new"):
        raise HTTPException(400, "bad status")
    if d.status == "not_interested" and d.reason not in DECLINE_REASONS:
        raise HTTPException(400, "pick a reason")
    if not db.get_job(job_id):
        raise HTTPException(404)
    db.set_decision(job_id, d.status, d.reason if d.status == "not_interested" else None, d.note)
    return {"ok": True}


@app.get("/api/reasons")
def reasons():
    return DECLINE_REASONS


# ---------------------------------------------------------------- tailored resume

@app.post("/api/jobs/{job_id}/tailor")
def tailor(job_id: int):
    job = db.get_job(job_id)
    resume = db.active_resume()
    if not job:
        raise HTTPException(404)
    if not resume:
        raise HTTPException(400, "Upload a resume first.")
    scored = next((j for j in db.list_jobs(resume["id"]) if j["id"] == job_id), None)
    max_jd_chars = load_settings().get("llm", {}).get("max_jd_chars", 8000)
    try:
        content = llm.tailor_resume(job, resume["content"], scored and scored["gaps"], max_jd_chars)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    db.save_tailored(job_id, resume["id"], content)
    return {"content": content}


@app.get("/api/jobs/{job_id}/tailor")
def get_tailored(job_id: int):
    t = db.latest_tailored(job_id)
    return {"content": t["content"] if t else None}


@app.get("/api/jobs/{job_id}/tailor.md", response_class=PlainTextResponse)
def download_tailored(job_id: int):
    t = db.latest_tailored(job_id)
    if not t:
        raise HTTPException(404)
    job = db.get_job(job_id)
    name = f"resume_{(job['company'] or 'job')}_{job_id}.md".replace(" ", "_")
    return PlainTextResponse(t["content"], headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- LLM connection

@app.get("/api/llm")
def llm_settings():
    s = llm.get_settings()
    return {**s.model_dump(exclude={"api_key"}), "has_api_key": bool(s.api_key)}


class LLMSettingsIn(BaseModel):
    base_url: str
    model: str = ""
    kind: str = "auto"
    api_key: str | None = None  # None = keep the saved key


@app.post("/api/llm")
def save_llm_settings(body: LLMSettingsIn):
    if body.kind not in ("auto", "openai", "ollama"):
        raise HTTPException(400, "kind must be auto, openai or ollama")
    current = llm.get_settings()
    llm.save_settings(llm.LLMSettings(
        base_url=body.base_url, model=body.model.strip(), kind=body.kind,
        api_key=current.api_key if body.api_key is None else body.api_key.strip(),
    ))
    return {"ok": True}


@app.post("/api/llm/test")
def test_llm():
    try:
        return llm.test_connection()
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------- pipeline

def _start(target, *args):
    if pipeline.state["running"]:
        raise HTTPException(409, "A run is already in progress.")

    def go():
        try:
            target(*args)
        except Exception:  # noqa: BLE001 - error is recorded in pipeline.state
            logging.exception("pipeline failed")

    # Mark as running before the thread starts so an immediate status poll sees it.
    pipeline.state.update(running=True, stage="starting", progress="", last_error=None)
    threading.Thread(target=go, daemon=True).start()
    return {"started": True}


class RunOptions(BaseModel):
    queries: list[str] | None = None     # None = use config.yaml
    posted_within: str | None = None     # key of POSTED_WITHIN; None = use config.yaml


@app.get("/api/pipeline/options")
def run_options():
    settings = load_settings()
    gs = (settings.get("sources") or {}).get("greenhouse_search") or {}
    return {
        "queries": pipeline.default_queries(settings),
        "posted_within": {k: label for k, (label, _) in POSTED_WITHIN.items()},
        "default_posted_within": gs.get("date_posted") if gs.get("date_posted") in POSTED_WITHIN else "any",
    }


@app.post("/api/pipeline/run")
def run_pipeline(opts: RunOptions | None = None, fetch: bool = True):
    opts = opts or RunOptions()
    if opts.posted_within and opts.posted_within not in POSTED_WITHIN:
        raise HTTPException(400, f"posted_within must be one of {list(POSTED_WITHIN)}")
    queries = [q.strip() for q in opts.queries or [] if q.strip()] or None
    return _start(pipeline.run, fetch, queries, opts.posted_within)


@app.post("/api/pipeline/rescore")
def rescore():
    return _start(pipeline.rescore_all)


@app.get("/api/pipeline/status")
def status():
    return {**pipeline.state, "runs": db.last_runs()}
