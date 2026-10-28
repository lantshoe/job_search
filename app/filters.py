"""Cheap keyword pre-filter that runs before any LLM call."""
import re
from datetime import datetime, timedelta, timezone

YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?(?:\s+of)?\s+(?:\w+\s+){0,3}experience", re.I)


def _contains_any(text: str, words: list[str]) -> str | None:
    text = text.lower()
    for w in words or []:
        if w.lower() in text:
            return w
    return None


def required_years(description: str) -> int | None:
    """Smallest 'N+ years of experience' mentioned — the JD's baseline requirement."""
    found = [int(m.group(1)) for m in YEARS_RE.finditer(description or "")]
    found = [n for n in found if 0 < n < 30]
    return min(found) if found else None


def check_title(title: str, cfg: dict) -> tuple[bool, str | None]:
    if cfg.get("title_include") and not _contains_any(title, cfg["title_include"]):
        return False, "title has no include keyword"
    if hit := _contains_any(title, cfg.get("title_exclude")):
        return False, f"title contains '{hit.strip()}'"
    return True, None


def check(job: dict, cfg: dict) -> tuple[bool, str | None]:
    """Return (passed, reason_if_dropped)."""
    title = job.get("title") or ""
    desc = job.get("description") or ""
    loc = job.get("location") or ""

    ok, reason = check_title(title, cfg)
    if not ok:
        return False, reason
    if hit := _contains_any(desc, cfg.get("description_exclude")):
        return False, f"description contains '{hit}'"
    if cfg.get("location_include") and not _contains_any(loc, cfg["location_include"]):
        return False, f"location '{loc}' not in allowed list"

    max_years = cfg.get("max_years_experience")
    if max_years is not None and (yrs := required_years(desc)) and yrs > max_years:
        return False, f"requires {yrs}+ years (max {max_years})"

    max_age = cfg.get("max_age_days")
    if max_age and job.get("posted_at"):
        posted = datetime.fromisoformat(job["posted_at"])
        if posted < datetime.now(timezone.utc) - timedelta(days=max_age):
            return False, f"posted more than {max_age} days ago"

    if len(desc) < 200:
        return False, "job description missing or too short"
    return True, None
