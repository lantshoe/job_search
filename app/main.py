import logging
import re
import threading
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, llm, pipeline, resume_render
from .config import DECLINE_REASONS, POSTED_WITHIN, ROOT, load_settings
from .resume_parser import extract_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Job Search Assistant")
db.init_db()

STATIC = ROOT / "static"
MAX_PROFILE_CHARS = 30000  # all resume versions combined; the oldest are cut beyond this
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    # Make the browser revalidate the page, CSS and JS so edits show up on a normal reload.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
    included = db.included_resumes()
    return {
        "resumes": db.list_resumes(),
        "facts": db.list_facts(),
        "total_chars": sum(len(r["content"]) for r in included),
        "max_chars": load_settings().get("llm", {}).get("max_profile_chars", MAX_PROFILE_CHARS),
    }


class Included(BaseModel):
    included: bool


@app.post("/api/resumes/{resume_id}/included")
def set_included(resume_id: int, body: Included):
    db.set_resume_included(resume_id, body.included)
    return {"ok": True}


@app.get("/api/resumes/{resume_id}")
def resume_text(resume_id: int):
    r = db.get_resume(resume_id)
    if not r:
        raise HTTPException(404)
    return {"id": r["id"], "filename": r["filename"], "content": r["content"]}


# ---------------------------------------------------------------- facts you confirmed

class FactIn(BaseModel):
    topic: str
    kind: str = "skill"
    has: bool = True
    detail: str = ""


@app.post("/api/facts")
def add_fact(body: FactIn):
    if not body.topic.strip():
        raise HTTPException(400, "Enter a skill or experience.")
    db.upsert_fact(body.topic, body.kind, body.has, body.detail)
    return {"ok": True}


@app.delete("/api/facts/{fact_id}")
def remove_fact(fact_id: int):
    db.delete_fact(fact_id)
    return {"ok": True}


# ---------------------------------------------------------------- jobs

@app.get("/api/jobs")
def jobs():
    threshold = load_settings().get("llm", {}).get("match_threshold", 70)
    n = len(db.included_resumes())
    return {"threshold": threshold, "resume": n and f"{n} in use", "jobs": db.list_jobs()}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return job


class Decision(BaseModel):
    status: str  # applied | not_interested | new
    reason: Optional[str] = None
    note: Optional[str] = None


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

def _max_jd_chars() -> int:
    return load_settings().get("llm", {}).get("max_jd_chars", 8000)


def _profile() -> str:
    max_chars = load_settings().get("llm", {}).get("max_profile_chars", MAX_PROFILE_CHARS)
    return llm.candidate_profile(db.included_resumes(), db.list_facts(), max_chars)


def _tailor_context(job_id: int) -> tuple[dict, int]:
    job = db.get_job(job_id)
    resumes = db.included_resumes()
    if not job:
        raise HTTPException(404)
    if not resumes:
        raise HTTPException(400, "Upload a resume first.")
    return job, resumes[0]["id"]


def _job_gaps(job_id: int) -> Optional[list]:
    scored = next((j for j in db.list_jobs() if j["id"] == job_id), None)
    return scored and scored["gaps"]


def _write_resume(job: dict, resume_id: int):
    data = llm.tailor_resume(job, _profile(), _job_gaps(job["id"]), _max_jd_chars())
    data, fit = _fit(data, shorten=lambda r, overflow: llm.shorten_resume(job, r, overflow))
    db.save_tailored(job["id"], resume_id, resume_render.to_markdown(data), {"resume": data, "fit": fit})
    db.add_message(job["id"], "assistant", "written", "Here is the resume for this job.", {
        "changes": data["changes"], "notes": data["notes"], "dropped": fit["dropped"],
    })


@app.post("/api/jobs/{job_id}/tailor/start")
def tailor_start(job_id: int):
    """Compare the job with everything known. Ask about what is missing; if nothing is, write the resume."""
    job, resume_id = _tailor_context(job_id)
    try:
        review = llm.review_job(job, _profile(), _max_jd_chars())
        db.add_message(job_id, "assistant", "review", review["assessment"], {"plan": review["plan"]})
        if review["questions"]:
            db.add_message(job_id, "assistant", "questions", "One quick check before I write it.",
                           {"questions": review["questions"]})
        else:
            _write_resume(job, resume_id)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return get_tailored(job_id)


class Answer(BaseModel):
    topic: str
    kind: str = "skill"
    has: Optional[bool] = None  # None = skipped; nothing is remembered
    detail: str = ""


class Answers(BaseModel):
    answers: list[Answer] = []


@app.post("/api/jobs/{job_id}/tailor")
def tailor(job_id: int, body: Optional[Answers] = None):
    """Remember your answers, then write the resume."""
    job, resume_id = _tailor_context(job_id)
    lines = []
    for a in body.answers if body else []:
        if a.has is None:
            lines.append(f"{a.topic}: skipped")
            continue
        db.upsert_fact(a.topic, a.kind, a.has, a.detail if a.has else "", job_id)
        lines.append(f"{a.topic}: {'yes' if a.has else 'no'}" + (f" — {a.detail.strip()}" if a.has and a.detail.strip() else ""))
    # Recorded before the LLM call, so a failure does not lose your answers or ask again.
    db.add_message(job_id, "user", "answers", "\n".join(lines) or "Write it without answering.")
    try:
        _write_resume(job, resume_id)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return get_tailored(job_id)


class ChatIn(BaseModel):
    message: str


@app.post("/api/jobs/{job_id}/tailor/chat")
def tailor_chat(job_id: int, body: ChatIn):
    job, resume_id = _tailor_context(job_id)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Type a message first.")
    t = db.latest_tailored(job_id)
    current = t["data"]["resume"] if t and t["data"] else None
    try:
        out = llm.chat_about_resume(job, _profile(), current, db.list_messages(job_id), message, _max_jd_chars())
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    db.add_message(job_id, "user", "chat", message)
    for f in out["facts"]:
        db.upsert_fact(f["topic"], f["kind"], f["has"], f["detail"], job_id)
    fit = None
    if out["resume"]:
        data = out["resume"]
        data["notes"] = data["notes"] or (current or {}).get("notes", [])
        data, fit = _fit(data, max_drops=0)
        db.save_tailored(job_id, resume_id, resume_render.to_markdown(data), {"resume": data, "fit": fit})
    db.add_message(job_id, "assistant", "chat", out["reply"], {
        "updated": bool(fit), "facts": [f["topic"] for f in out["facts"]],
    })
    return get_tailored(job_id)


class ResumeEdit(BaseModel):
    resume: dict


@app.put("/api/jobs/{job_id}/tailor")
def edit_tailored(job_id: int, body: ResumeEdit):
    """Save your edits as a new version. The page fit is re-checked, but nothing you wrote is cut."""
    t = db.latest_tailored(job_id)
    if not t or not t["data"]:
        raise HTTPException(404, "No editable tailored resume for this job.")
    data = resume_render.normalize(body.resume)
    data["notes"] = t["data"]["resume"].get("notes", [])
    data["changes"] = t["data"]["resume"].get("changes", [])
    data, fit = _fit(data, max_drops=0)
    fit["edited"] = True
    db.save_tailored(job_id, t["resume_id"], resume_render.to_markdown(data), {"resume": data, "fit": fit})
    return get_tailored(job_id)


def _fit(data: dict, **kwargs) -> tuple[dict, dict]:
    page_size = _page_size()
    try:
        return resume_render.fit(data, page_size, **kwargs)
    except resume_render.RenderError as e:
        # No PDF engine: still keep the content; Word export works without Chrome.
        mid = len(resume_render.LEVELS) // 2
        return data, {"page_size": page_size, "level": mid, "font": resume_render.LEVELS[mid]["font"],
                      "pages": None, "error": str(e), "shortened": False, "dropped": []}


def _latest_tailored(job_id: int) -> Optional[dict]:
    """The newest version, with its page fit re-checked if it was saved under an older template."""
    t = db.latest_tailored(job_id)
    data = t and t["data"]
    if data and data["fit"].get("layout") != resume_render.LAYOUT and not data["fit"].get("error"):
        _, new = _fit(data["resume"], max_drops=0)  # nothing is cut; the size just adapts
        data["fit"].update({k: new[k] for k in ("level", "font", "pages")}, layout=resume_render.LAYOUT)
        db.update_tailored_data(t["id"], data)
    return t


def _page_size() -> str:
    size = str(load_settings().get("resume", {}).get("page_size", "letter")).lower()
    return size if size in resume_render.PAGE_SIZES else "letter"


@app.get("/api/jobs/{job_id}/tailor")
def get_tailored(job_id: int):
    messages = db.list_messages(job_id)
    # Questions stay open until the next message answers them.
    pending = messages[-1]["data"]["questions"] if messages and messages[-1]["kind"] == "questions" else []
    t = _latest_tailored(job_id)
    if not t:
        return {"content": None, "messages": messages, "pending": pending}
    data = t["data"] or {}
    return {
        "messages": messages,
        "pending": pending,
        "id": t["id"],
        "content": t["content"],
        "created_at": t["created_at"],
        "has_layout": bool(data),
        "fit": data.get("fit"),
        "resume": data.get("resume"),
        "notes": (data.get("resume") or {}).get("notes", []),
    }


def _laid_out(job_id: int) -> tuple[dict, dict, str]:
    t = _latest_tailored(job_id)
    if not t:
        raise HTTPException(404, "No tailored resume for this job yet.")
    if not t["data"]:
        raise HTTPException(409, "This resume was written before PDF/Word export. Regenerate it.")
    job = db.get_job(job_id)
    name = t["data"]["resume"].get("name") or "Resume"
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "", f"{name} - {job['company'] or 'job'}").strip() or f"resume_{job_id}"
    return t, t["data"], stem


def _attachment(stem: str, ext: str, download: bool) -> dict:
    kind = "attachment" if download else "inline"
    return {"Content-Disposition": f'{kind}; filename="{stem}.{ext}"'}


_pdf_cache: dict[int, bytes] = {}  # tailored resume id -> PDF; the preview reloads it often


@app.get("/api/jobs/{job_id}/tailor.pdf")
def tailored_pdf(job_id: int, download: bool = False):
    t, data, stem = _laid_out(job_id)
    if t["id"] not in _pdf_cache:
        fit = data["fit"]
        try:
            pdf = resume_render.render_pdf(data["resume"], resume_render.LEVELS[fit["level"]], fit["page_size"])
        except resume_render.RenderError as e:
            raise HTTPException(503, str(e))
        if len(_pdf_cache) > 20:
            _pdf_cache.clear()
        _pdf_cache[t["id"]] = pdf
    return Response(_pdf_cache[t["id"]], media_type="application/pdf", headers=_attachment(stem, "pdf", download))


@app.get("/api/jobs/{job_id}/tailor.docx")
def tailored_docx(job_id: int):
    _, data, stem = _laid_out(job_id)
    fit = data["fit"]
    docx = resume_render.to_docx(data["resume"], resume_render.LEVELS[fit["level"]], fit["page_size"])
    return Response(docx, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers=_attachment(stem, "docx", True))


class ResumeHtml(BaseModel):
    resume: dict
    level: Optional[int] = None  # None = the smallest readable size
    editable: bool = False


@app.post("/api/resume/html")
def resume_html(body: ResumeHtml):
    """The page as HTML, for the on-page editor and its live one-page estimate."""
    levels = resume_render.LEVELS
    tight = len(levels) - 1
    index = tight if body.level is None else max(0, min(tight, body.level))
    level, page_size = levels[index], _page_size()
    # The editor keeps blank entries you just added; the measured copy matches what prints.
    resume = {**body.resume} if body.editable else resume_render.normalize(body.resume)
    _, page_h = resume_render.PAGE_SIZES[page_size]
    return {
        "html": resume_render.to_html(resume, level, page_size, editable=body.editable),
        "level": index,
        "font": level["font"],
        "line_px": level["font"] * level["line"] * 96 / 72,
        "content_px": (page_h - 2 * level["margin"]) * 96,
        "margin_px": level["margin"] * 96,
        "page_px": resume_render.PAGE_SIZES[page_size][0] * 96,
    }


@app.get("/api/jobs/{job_id}/tailor.html")
def tailored_docs_html(job_id: int):
    _, data, _ = _laid_out(job_id)
    return Response(resume_render.to_docs_html(data["resume"]), media_type="text/html")


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
    api_key: Optional[str] = None  # None = keep the saved key


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
    queries: Optional[list[str]] = None     # None = use config.yaml
    posted_within: Optional[str] = None     # key of POSTED_WITHIN; None = use config.yaml


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
def run_pipeline(opts: Optional[RunOptions] = None, fetch: bool = True):
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
