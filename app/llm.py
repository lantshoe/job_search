"""LLM calls to a self-hosted model (e.g. on Colab behind a Cloudflare tunnel).

Two server types are supported and auto-detected from the URL:
  - "openai": OpenAI-compatible chat API (vLLM, llama.cpp server, LM Studio, Ollama's /v1, TGI)
  - "ollama": Ollama's native API
"""
import json
import re

import httpx
from pydantic import BaseModel

from . import db
from .config import DECLINE_REASONS, LLM_API_KEY, LLM_BASE_URL, LLM_KIND, LLM_MODEL

TIMEOUT = httpx.Timeout(600, connect=20)  # Colab models can be slow; resumes take a while


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- connection settings

class LLMSettings(BaseModel):
    base_url: str = ""
    model: str = ""      # empty = use the first model the server lists
    kind: str = "auto"   # auto | openai | ollama
    api_key: str = ""


def get_settings() -> LLMSettings:
    """Settings saved from the web page win over .env, so a new tunnel URL can be pasted in the UI."""
    saved = db.get_setting("llm") or {}
    return LLMSettings(
        base_url=saved.get("base_url") or LLM_BASE_URL,
        model=saved.get("model") or LLM_MODEL,
        kind=saved.get("kind") or LLM_KIND,
        api_key=saved.get("api_key") or LLM_API_KEY,
    )


def save_settings(s: LLMSettings):
    s.base_url = s.base_url.strip().rstrip("/")
    db.set_setting("llm", s.model_dump())
    _detected.clear()


_detected: dict[tuple, tuple[str, str]] = {}


def _root(url: str) -> str:
    """Strip a trailing /v1 or /api so both 'https://x.trycloudflare.com' and '.../v1' work."""
    return re.sub(r"/(v1|api)/?$", "", url.strip().rstrip("/"))


def _headers(s: LLMSettings) -> dict:
    h = {"Content-Type": "application/json"}
    if s.api_key:
        h["Authorization"] = f"Bearer {s.api_key}"
    return h


def detect(s: LLMSettings) -> tuple[str, str]:
    """Return (kind, model), probing the server once per settings combination."""
    if not s.base_url:
        raise LLMError("No LLM link set. Click 'LLM' at the top of the page and paste your Colab link.")
    key = (s.base_url, s.kind, s.model, s.api_key)
    if key not in _detected:
        _detected[key] = _probe(s)
    return _detected[key]


def _probe(s: LLMSettings) -> tuple[str, str]:
    root = _root(s.base_url)
    with httpx.Client(timeout=20, headers=_headers(s)) as c:
        if s.kind in ("auto", "ollama"):
            try:
                r = c.get(f"{root}/api/tags")
                if r.status_code == 200 and "models" in r.json():
                    names = [m["name"] for m in r.json()["models"]]
                    if not s.model and not names:
                        raise LLMError("Ollama is running but has no models pulled (run `ollama pull <model>` in Colab).")
                    return "ollama", s.model or names[0]
            except (httpx.HTTPError, ValueError):
                pass
        if s.kind in ("auto", "openai"):
            try:
                r = c.get(f"{root}/v1/models")
                if r.status_code == 200:
                    ids = [m["id"] for m in r.json().get("data", [])]
                    return "openai", s.model or (ids[0] if ids else "default")
            except (httpx.HTTPError, ValueError):
                pass
    raise LLMError(
        f"Could not reach an Ollama or OpenAI-compatible server at {root}. "
        "Check the Colab notebook is still running and the Cloudflare link is current."
    )


def chat(system: str, user: str, json_mode: bool = False, max_tokens: int = 2048) -> str:
    s = get_settings()
    kind, model = detect(s)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    root = _root(s.base_url)
    with httpx.Client(timeout=TIMEOUT, headers=_headers(s)) as c:
        try:
            if kind == "ollama":
                body = {
                    "model": model, "messages": messages, "stream": False,
                    "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 16384},
                }
                if json_mode:
                    body["format"] = "json"
                r = c.post(f"{root}/api/chat", json=body)
                r.raise_for_status()
                text = r.json()["message"]["content"]
            else:
                body = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                r = c.post(f"{root}/v1/chat/completions", json=body)
                if r.status_code == 400 and json_mode:
                    # Some servers don't support response_format; the prompt still asks for JSON.
                    body.pop("response_format")
                    r = c.post(f"{root}/v1/chat/completions", json=body)
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as e:
            raise LLMError(f"LLM server returned {e.response.status_code}: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach the LLM server: {e}")
    # Reasoning models (Qwen3, DeepSeek-R1) put their thinking in <think> tags.
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def test_connection() -> dict:
    _detected.clear()
    s = get_settings()
    kind, model = detect(s)
    reply = chat("You are a helpful assistant.", "Reply with exactly: OK", max_tokens=200)
    return {"kind": kind, "model": model, "reply": reply[:200]}


# ---------------------------------------------------------------- scoring

GAP_CATEGORIES = {
    "experience_years", "skills", "seniority", "domain", "education",
    "sponsorship", "location", "language", "other",
}

SCORE_INSTRUCTIONS = """You screen job postings for one candidate. Judge how well the candidate's resume fits the job description and reply with a JSON object only — no other text.

Scoring guide:
- 85-100 "strong_match": meets essentially all must-haves; would likely get an interview.
- 70-84 "match": meets most must-haves; gaps are minor or learnable.
- 50-69 "weak_match": real gaps in must-haves (years, core stack, seniority) but not disqualifying.
- 0-49 "no_match": hard blockers (much more experience required, different field, sponsorship refused, location impossible).

Be concrete about gaps: name the specific requirement and what the resume shows instead. Judge only what the resume actually shows; do not assume unlisted skills. Missing nice-to-haves are "low" severity.

The candidate's past decisions show what they actually want. When a job resembles ones they declined, lower the score and say why in the summary; when it resembles ones they applied to, that is a positive signal.

Reply with exactly this JSON shape:
{
  "score": <integer 0-100>,
  "verdict": "strong_match" | "match" | "weak_match" | "no_match",
  "summary": "<2-3 sentences explaining the score>",
  "matched_skills": ["<requirement from the JD the resume clearly satisfies>", ...],
  "gaps": [{"category": "experience_years" | "skills" | "seniority" | "domain" | "education" | "sponsorship" | "location" | "language" | "other",
            "detail": "<what the JD asks for that the resume does not show>",
            "severity": "low" | "medium" | "high"}, ...],
  "sponsorship": "offered" | "not_offered" | "unclear",
  "years_required": <minimum years of experience the JD asks for, or null>
}"""


def _preferences_block(history: list[dict]) -> str:
    if not history:
        return "No past decisions yet."
    lines = []
    for h in history:
        what = f"{h['title']} at {h['company'] or '?'} ({h['location'] or '?'})"
        if h["status"] == "applied":
            lines.append(f"- APPLIED: {what}")
        else:
            reason = DECLINE_REASONS.get(h["decline_reason"] or "", h["decline_reason"] or "no reason")
            note = f" — note: {h['decline_note']}" if h["decline_note"] else ""
            lines.append(f"- DECLINED ({reason}): {what}{note}")
    return "\n".join(lines)


def _format_job(job: dict, max_chars: int) -> str:
    desc = job.get("description") or ""
    if len(desc) > max_chars:
        desc = desc[:max_chars] + "\n[... description truncated to fit the model's context ...]"
    return (
        f"Title: {job['title']}\nCompany: {job.get('company') or 'unknown'}\n"
        f"Location: {job.get('location') or 'unknown'}\nSalary: {job.get('salary') or 'not listed'}\n\n"
        f"Job description:\n{desc}"
    )


def build_score_system(resume_text: str, history: list[dict]) -> str:
    return (
        f"{SCORE_INSTRUCTIONS}\n\n<resume>\n{resume_text}\n</resume>\n\n"
        f"<past_decisions>\n{_preferences_block(history)}\n</past_decisions>"
    )


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def _normalize(raw: dict) -> dict:
    """Small local models drift from the schema; coerce what we can instead of failing."""
    try:
        score = max(0, min(100, int(float(raw.get("score", 0)))))
    except (TypeError, ValueError):
        raise ValueError("score is not a number")
    verdict = raw.get("verdict")
    if verdict not in ("strong_match", "match", "weak_match", "no_match"):
        verdict = "strong_match" if score >= 85 else "match" if score >= 70 else "weak_match" if score >= 50 else "no_match"
    gaps = []
    for g in raw.get("gaps") or []:
        if isinstance(g, str):
            g = {"detail": g}
        if not isinstance(g, dict) or not g.get("detail"):
            continue
        cat = str(g.get("category", "other")).lower().replace(" ", "_")
        sev = str(g.get("severity", "medium")).lower()
        gaps.append({
            "category": cat if cat in GAP_CATEGORIES else "other",
            "detail": str(g["detail"]),
            "severity": sev if sev in ("low", "medium", "high") else "medium",
        })
    sponsorship = raw.get("sponsorship")
    years = raw.get("years_required")
    try:
        years = int(years) if years not in (None, "", "null") else None
    except (TypeError, ValueError):
        years = None
    return {
        "score": score,
        "verdict": verdict,
        "summary": str(raw.get("summary") or ""),
        "matched_skills": [str(m) for m in raw.get("matched_skills") or [] if m],
        "gaps": gaps,
        "sponsorship": sponsorship if sponsorship in ("offered", "not_offered", "unclear") else "unclear",
        "years_required": years,
    }


def score_job(job: dict, system: str, max_jd_chars: int = 8000) -> dict:
    user = _format_job(job, max_jd_chars)
    reply = chat(system, user, json_mode=True)
    try:
        return _normalize(_extract_json(reply))
    except (ValueError, json.JSONDecodeError) as e:
        # One retry with the error pointed out; small models usually fix it.
        retry = f"{user}\n\nYour previous reply could not be parsed ({e}). Reply with only the JSON object."
        reply = chat(system, retry, json_mode=True)
        try:
            return _normalize(_extract_json(reply))
        except (ValueError, json.JSONDecodeError) as e2:
            raise LLMError(f"Model did not return valid JSON: {e2}; reply began: {reply[:150]!r}")


# ---------------------------------------------------------------- tailored resume

TAILOR_INSTRUCTIONS = """You rewrite a candidate's resume to target one specific job. Output the complete resume in clean Markdown, ready to paste into a document, and nothing else.

Rules:
- Never invent employers, titles, dates, degrees, certifications, metrics or skills. Every fact must come from the original resume.
- You may reorder sections and bullets, rephrase bullets to use the job description's terminology where it is truthfully the same thing, tighten wording, and drop content irrelevant to this role.
- Lead with what this job cares about most. Put the most relevant experience and skills first.
- Keep it to what fits on one to two pages.
- After the resume, add a section "## Notes for you" (separated by a horizontal rule) listing: the gaps you could not cover truthfully, and any questions to answer before sending (e.g. "Do you have Kubernetes experience not listed?")."""


def tailor_resume(job: dict, resume_text: str, gaps: list[dict] | None = None, max_jd_chars: int = 8000) -> str:
    gap_text = ""
    if gaps:
        gap_text = "\n\nKnown gaps from screening:\n" + "\n".join(
            f"- [{g['severity']}] {g['category']}: {g['detail']}" for g in gaps
        )
    user = (
        f"<original_resume>\n{resume_text}\n</original_resume>\n\n"
        f"<job>\n{_format_job(job, max_jd_chars)}\n</job>{gap_text}"
    )
    text = chat(TAILOR_INSTRUCTIONS, user, max_tokens=4096)
    if not text:
        raise LLMError("Model returned an empty reply")
    return text
