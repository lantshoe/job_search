"""LLM calls to a self-hosted model (e.g. on Colab behind a Cloudflare tunnel).

Two server types are supported and auto-detected from the URL:
  - "openai": OpenAI-compatible chat API (vLLM, llama.cpp server, LM Studio, Ollama's /v1, TGI)
  - "ollama": Ollama's native API
"""
import json
import re
from typing import Optional

import httpx
from pydantic import BaseModel

from . import db, jd_parse, resume_render
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

SCORE_INSTRUCTIONS = """You screen job postings for one candidate. Judge how well the candidate fits the job description and reply with a JSON object only — no other text.

Scoring guide:
- 85-100 "strong_match": meets essentially all must-haves; would likely get an interview.
- 70-84 "match": meets most must-haves; gaps are minor or learnable.
- 50-69 "weak_match": real gaps in must-haves (years, core stack, seniority) but not disqualifying.
- 0-49 "no_match": hard blockers (much more experience required, different field, sponsorship refused, location impossible).

Be concrete about gaps: name the specific requirement and what the resume shows instead. The candidate section holds several resume versions of the same person, each with a different focus, plus facts they confirmed directly. Judge against all of it combined, so a skill in any version counts; do not assume unlisted skills, and treat anything marked DOES NOT HAVE as missing. When the job lists MUST-HAVE and NICE-TO-HAVE requirements separately, base the score mainly on the must-haves; a missing nice-to-have is "low" severity and should barely move the score.

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


def _requirements_block(desc: str) -> str:
    """Must-have vs nice-to-have bullets parsed from the JD's headings, when it has them."""
    s = jd_parse.sections(desc)
    parts = []
    if s["required"]:
        parts.append("MUST-HAVE requirements:\n" + "\n".join(f"- {b}" for b in s["required"]))
    if s["bonus"]:
        parts.append("NICE-TO-HAVE (missing these is low severity):\n" + "\n".join(f"- {b}" for b in s["bonus"]))
    return "\n\n".join(parts)


def _format_job(job: dict, max_chars: int) -> str:
    desc = job.get("description") or ""
    reqs = _requirements_block(desc)
    if len(desc) > max_chars:
        desc = desc[:max_chars] + "\n[... description truncated to fit the model's context ...]"
    return (
        f"Title: {job['title']}\nCompany: {job.get('company') or 'unknown'}\n"
        f"Location: {job.get('location') or 'unknown'}\nSalary: {job.get('salary') or 'not listed'}\n\n"
        + (f"{reqs}\n\n" if reqs else "")
        + f"Full job description:\n{desc}"
    )


def candidate_profile(resumes: list[dict], facts: list[dict], max_chars: int = 30000) -> str:
    """Everything known about the candidate: all resume versions (newest first) and confirmed facts.

    Facts always go in. If the resumes exceed max_chars, the oldest versions are cut, with a marker.
    """
    parts, room = [], max_chars
    for r in resumes:
        text = r["content"]
        if len(text) > room:
            text = text[:max(room, 0)] + "\n[... cut to fit the model's context ...]"
        room -= len(r["content"])
        parts.append(f'<resume_version file="{r["filename"]}" uploaded="{r["created_at"][:10]}">\n{text}\n</resume_version>')
    if facts:
        lines = []
        for f in facts:
            if f["has"]:
                lines.append(f"- HAS {f['topic']}" + (f": {f['detail']}" if f["detail"] else " (no details given)"))
            else:
                lines.append(f"- DOES NOT HAVE {f['topic']}")
        parts.append("<confirmed_by_candidate>\n" + "\n".join(lines) + "\n</confirmed_by_candidate>")
    return "\n\n".join(parts)


def build_score_system(profile: str, history: list[dict]) -> str:
    return (
        f"{SCORE_INSTRUCTIONS}\n\n<candidate>\n{profile}\n</candidate>\n\n"
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


def _chat_json(system: str, user: str, parse, max_tokens: int = 2048):
    reply = chat(system, user, json_mode=True, max_tokens=max_tokens)
    try:
        return parse(_extract_json(reply))
    except (ValueError, json.JSONDecodeError) as e:
        # One retry with the error pointed out; small models usually fix it.
        retry = f"{user}\n\nYour previous reply could not be parsed ({e}). Reply with only the JSON object."
        reply = chat(system, retry, json_mode=True, max_tokens=max_tokens)
        try:
            return parse(_extract_json(reply))
        except (ValueError, json.JSONDecodeError) as e2:
            raise LLMError(f"Model did not return valid JSON: {e2}; reply began: {reply[:150]!r}")


def score_job(job: dict, system: str, max_jd_chars: int = 8000) -> dict:
    return _chat_json(system, _format_job(job, max_jd_chars), _normalize)


# ---------------------------------------------------------------- tailored resume

RESUME_SHAPE = """{
  "name": "<full name, exactly as in the resume>",
  "headline": "<short professional title aimed at this job, or \\"\\">",
  "contact": ["<email>", "<phone>", "<city, state>", "<linkedin / github / website>"],
  "summary": "<at most 3 sentences>",
  "skills": [{"label": "<e.g. Languages>", "items": "<comma-separated, most relevant first>"}],
  "experience": [{"title": "", "company": "", "location": "", "dates": "<as in the resume, e.g. Jan 2022 – Present>",
                  "bullets": ["<one sentence>"]}],
  "projects": [{"name": "", "detail": "<tech used or one-line context>", "dates": "", "bullets": ["<one sentence>"]}],
  "education": [{"school": "", "degree": "", "location": "", "dates": "", "detail": "<GPA, honors, coursework, or \\"\\">"}],
  "extra": [{"label": "<e.g. Certifications, Languages, Awards>", "items": "<comma-separated>"}],
  "changes": ["<for the candidate only, not printed: one thing you emphasized, moved or left out for this job, and why>"],
  "notes": ["<for the candidate only, not printed: a gap you could not cover truthfully, or a question to answer before sending>"]
}"""

FACT_RULES = """- The candidate section holds several resume versions of the same person, each with a different focus, plus facts they confirmed directly. Draw on all of them and pick what suits this job. Where versions disagree on a fact such as a date, use the newest version.
- Be an active advocate: make the candidate's real work read as a fit for this job. Describe it in the job's language and bring out the parts that overlap. For example, a customer outreach system can be written up around audience targeting and campaign delivery for an advertising role.
- A near miss counts as a fit. If the job asks for 5 years and they have 4+, present their experience confidently as "4+ years" and do not flag it as a gap. Treat equivalent tools and related domains the same way: lead with what they do have.
- The underlying work must be real. Never invent employers, titles, dates, degrees, certifications or metrics, and never name a tool or claim a number of years they do not have. Copy names, titles, dates and contact details exactly.
- A fact marked HAS may be used. With details, write it into the relevant role using those details; with "no details given", only list it under skills and never build a bullet on it. Never claim anything marked DOES NOT HAVE."""

TAILOR_INSTRUCTIONS = f"""You tailor a candidate's resume to one specific job. Reply with a JSON object only — no other text. The app lays it out on a single page, so keep it short enough to fit one page.

Rules:
{FACT_RULES}
- You may reorder sections and bullets, rephrase bullets to use the job description's terminology where it is truthfully the same thing, tighten wording, and leave out what is irrelevant to this job.
- Lead with what this job cares about most: the most relevant skills and bullets first.
- In "changes", name each bullet you reframed, so the candidate can check it. In "notes", list only real blockers, not near misses.
- One-page limits: summary at most 3 sentences; at most 5 skill lines; at most 5 bullets for the most recent role and 3 for older ones; at most 3 projects with 2 bullets each, only relevant ones; every bullet one sentence of at most 28 words.
- Use "" or [] for anything the resume does not have. Do not use Markdown inside the values.

Reply with exactly this JSON shape:
{RESUME_SHAPE}"""

SHORTEN_INSTRUCTIONS = f"""You shorten a tailored resume that is slightly too long for one page. Reply with a JSON object only, in exactly the same shape as the input.

Cut about {{percent}}% of the text: drop the bullets, projects, skills or extra lines least relevant to the job, and tighten long bullets. Keep every remaining fact unchanged — never add or alter facts. Keep "notes" as they are.

Shape:
{RESUME_SHAPE}"""


def _gap_text(gaps: Optional[list[dict]]) -> str:
    if not gaps:
        return ""
    return "\n\nKnown gaps from screening:\n" + "\n".join(
        f"- [{g['severity']}] {g['category']}: {g['detail']}" for g in gaps
    )


def _parse_resume(raw: dict) -> dict:
    r = resume_render.normalize(raw)
    if not (r["experience"] or r["education"]):
        raise ValueError("no experience or education in the reply")
    return r


def tailor_resume(job: dict, profile: str, gaps: Optional[list[dict]] = None, max_jd_chars: int = 8000) -> dict:
    user = (
        f"<candidate>\n{profile}\n</candidate>\n\n"
        f"<job>\n{_format_job(job, max_jd_chars)}\n</job>{_gap_text(gaps)}"
    )
    return _chat_json(TAILOR_INSTRUCTIONS, user, _parse_resume, max_tokens=4096)


def shorten_resume(job: dict, resume: dict, overflow: float) -> dict:
    """overflow = share of the text that spilled onto page 2."""
    percent = max(10, min(40, round(overflow * 100) + 5))
    system = SHORTEN_INSTRUCTIONS.replace("{percent}", str(percent))
    user = (
        f"<job>\nTitle: {job['title']}\nCompany: {job.get('company') or 'unknown'}\n\n"
        f"{_requirements_block(job.get('description') or '') or (job.get('description') or '')[:3000]}\n</job>\n\n"
        f"<resume_json>\n{json.dumps(resume, ensure_ascii=False)}\n</resume_json>"
    )
    shorter = _chat_json(system, user, _parse_resume, max_tokens=4096)
    shorter["notes"] = resume.get("notes", [])
    shorter["changes"] = resume.get("changes", [])
    return shorter


# ---------------------------------------------------------------- reviewing and chatting with the candidate

REVIEW_INSTRUCTIONS = """You compare a job description with everything known about a candidate, before their tailored resume is written. Reply with a JSON object only — no other text.

The candidate section holds several resume versions of the same person plus facts they confirmed.

Asking is a last resort. Almost always return an empty list, because the writer can reframe related experience in the job's terms and will handle near misses by itself.

Ask only when ALL of these are true:
- It is a specific named tool, technology or certification that the job truly requires.
- Nothing in the candidate section is the same, equivalent or related to it.
- A yes would change what goes on the resume.

Never ask about:
- Years of experience. The resumes already show them, and being a little short still counts as a fit.
- Anything with related experience in the resumes, such as another cloud provider, another database, deeper use of a skill they list, or a neighboring domain. Plan to reframe it instead.
- Nice-to-haves, soft skills, company type or industry background.
- Visa, salary or location.
Never ask open-ended questions like "describe" or "give examples". Each question must be answerable with yes or no. At most 3 questions.

Reply with exactly this JSON shape:
{
  "assessment": "<2-3 sentences: how well the candidate lines up with this job>",
  "plan": ["<one thing you will emphasize or reframe, e.g. which real experience covers which requirement>"],
  "questions": [{"topic": "<short name, e.g. MySQL>", "kind": "skill" | "experience",
                 "question": "<a direct question, e.g. The job requires MySQL. Have you used it, and where?>",
                 "importance": "must_have" | "nice_to_have"}]
}"""

CHAT_INSTRUCTIONS = f"""You help a candidate refine their tailored resume for one job, in a conversation. Reply with a JSON object only — no other text.

Rules:
{FACT_RULES}
- What the candidate tells you in this conversation also counts as fact. Use only what they actually said; never fill in details for them.
- Answer honestly. If asked to add something they do not have, say so and suggest a truthful alternative.
- Change the resume only when the candidate's message calls for it. When you do, return the complete updated resume and keep everything else as it was.

Reply with exactly this JSON shape:
{{
  "reply": "<your answer to the candidate, in plain text>",
  "resume": <null if nothing changed, otherwise the complete resume in the shape below>,
  "facts": [{{"topic": "<short name>", "kind": "skill" | "experience", "has": true | false, "detail": "<what they said>"}}]
}}
"facts" lists only skills or experience the candidate stated in their latest message, to remember for future jobs; use [] if none.

Resume shape:
{RESUME_SHAPE}"""


def _parse_review(raw: dict) -> dict:
    questions = []
    for q in raw.get("questions") or []:
        if isinstance(q, dict) and q.get("topic") and q.get("question"):
            questions.append({
                "topic": str(q["topic"]).strip(),
                "kind": "experience" if q.get("kind") == "experience" else "skill",
                "question": str(q["question"]).strip(),
                "importance": "nice_to_have" if q.get("importance") == "nice_to_have" else "must_have",
            })
    plan = raw.get("plan") or []
    return {
        "assessment": str(raw.get("assessment") or "").strip(),
        "plan": [str(p).strip() for p in ([plan] if isinstance(plan, str) else plan) if p],
        "questions": questions,
    }


# Questions the writer should settle by itself: years, open-ended prompts, and anything the
# model itself admits has related experience.
_NEEDLESS_QUESTION = re.compile(
    r"\byears?\b|\b(describe|give examples|walk me through|tell me about|explain)\b|\bcan you confirm\b"
    r"|\b(beyond|besides|other than)\b|\byour resumes? (lists?|shows?|mentions?|has|have)\b",
    re.I,
)


def new_questions(questions: list[dict], profile: str) -> list[dict]:
    """Models over-ask even when told not to, so only blocking yes/no questions on new topics get through."""
    out, seen = [], set()
    for q in questions:
        key = q["topic"].lower()
        mentioned = re.search(rf"(?<!\w){re.escape(q['topic'])}(?!\w)", profile, re.I)
        if key in seen or mentioned or q["importance"] != "must_have" or _NEEDLESS_QUESTION.search(q["question"]):
            continue
        seen.add(key)
        out.append(q)
    return out[:3]


def review_job(job: dict, profile: str, max_jd_chars: int = 8000) -> dict:
    # The strict gaps from scoring are left out on purpose: they only prompt needless questions.
    user = f"<candidate>\n{profile}\n</candidate>\n\n<job>\n{_format_job(job, max_jd_chars)}\n</job>"
    review = _chat_json(REVIEW_INSTRUCTIONS, user, _parse_review)
    review["questions"] = new_questions(review["questions"], profile)
    return review


def _parse_chat(raw: dict) -> dict:
    reply = str(raw.get("reply") or "").strip()
    if not reply:
        raise ValueError("no reply in the JSON")
    facts = []
    for f in raw.get("facts") or []:
        if isinstance(f, dict) and f.get("topic") and isinstance(f.get("has"), bool):
            facts.append({
                "topic": str(f["topic"]).strip(), "has": f["has"], "detail": str(f.get("detail") or "").strip(),
                "kind": "experience" if f.get("kind") == "experience" else "skill",
            })
    resume = None
    if isinstance(raw.get("resume"), dict):
        resume = resume_render.normalize(raw["resume"])
        if not (resume["experience"] or resume["education"]):
            resume = None
    return {"reply": reply, "resume": resume, "facts": facts}


def chat_about_resume(job: dict, profile: str, current: Optional[dict], history: list[dict], message: str,
                      max_jd_chars: int = 8000) -> dict:
    past = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history[-12:])
    user = (
        f"<candidate>\n{profile}\n</candidate>\n\n<job>\n{_format_job(job, max_jd_chars)}\n</job>\n\n"
        f"<current_resume>\n{json.dumps(current, ensure_ascii=False) if current else 'Not written yet.'}\n</current_resume>\n\n"
        f"<conversation_so_far>\n{past or 'None yet.'}\n</conversation_so_far>\n\n"
        f"The candidate's latest message:\n{message}"
    )
    return _chat_json(CHAT_INSTRUCTIONS, user, _parse_chat, max_tokens=4096)
