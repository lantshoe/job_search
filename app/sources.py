"""Job source adapters. Each returns a list of normalized job dicts:

    {source, external_id, title, company, location, url, description, salary, posted_at}

To add a source, write a `fetch_<name>(client, cfg)` function and register it in SOURCES.
"""
import html
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(raw) if "&lt;" in raw else raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _iso(value) -> str | None:
    """Normalize unix seconds/ms or ISO strings to an ISO-8601 UTC string."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            value /= 1000
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def fetch_remotive(client: httpx.Client, cfg: dict) -> list[dict]:
    jobs = {}
    for q in cfg.get("queries") or [""]:
        r = client.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": q, "limit": cfg.get("limit_per_query", 50)},
        )
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            jobs[str(j["id"])] = {
                "source": "remotive",
                "external_id": str(j["id"]),
                "title": j.get("title", ""),
                "company": j.get("company_name"),
                "location": j.get("candidate_required_location") or "Remote",
                "url": j.get("url"),
                "description": html_to_text(j.get("description")),
                "salary": j.get("salary") or None,
                "posted_at": _iso(j.get("publication_date")),
            }
    return list(jobs.values())


def fetch_arbeitnow(client: httpx.Client, cfg: dict) -> list[dict]:
    out = []
    for page in range(1, cfg.get("pages", 1) + 1):
        r = client.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        r.raise_for_status()
        for j in r.json().get("data", []):
            loc = j.get("location") or ""
            if j.get("remote"):
                loc = f"{loc} (Remote)".strip()
            out.append({
                "source": "arbeitnow",
                "external_id": j["slug"],
                "title": j.get("title", ""),
                "company": j.get("company_name"),
                "location": loc,
                "url": j.get("url"),
                "description": html_to_text(j.get("description")),
                "salary": None,
                "posted_at": _iso(j.get("created_at")),
            })
    return out


def fetch_greenhouse(client: httpx.Client, cfg: dict) -> list[dict]:
    out = []
    for slug in cfg.get("companies", []):
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        if r.status_code == 404:
            log.warning("greenhouse board not found: %s", slug)
            continue
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            out.append({
                "source": "greenhouse",
                "external_id": f"{slug}:{j['id']}",
                "title": j.get("title", ""),
                "company": slug,
                "location": (j.get("location") or {}).get("name"),
                "url": j.get("absolute_url"),
                "description": html_to_text(j.get("content")),
                "salary": None,
                "posted_at": _iso(j.get("first_published") or j.get("updated_at")),
            })
    return out


def fetch_lever(client: httpx.Client, cfg: dict) -> list[dict]:
    out = []
    for slug in cfg.get("companies", []):
        r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        if r.status_code == 404:
            log.warning("lever board not found: %s", slug)
            continue
        r.raise_for_status()
        for j in r.json():
            sections = [j.get("descriptionPlain") or ""]
            for lst in j.get("lists", []):
                sections.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            sections.append(j.get("additionalPlain") or "")
            salary = j.get("salaryRange") or {}
            out.append({
                "source": "lever",
                "external_id": f"{slug}:{j['id']}",
                "title": j.get("text", ""),
                "company": slug,
                "location": (j.get("categories") or {}).get("location"),
                "url": j.get("hostedUrl") or j.get("applyUrl"),
                "description": "\n\n".join(s for s in sections if s).strip(),
                "salary": f"{salary.get('min')}-{salary.get('max')} {salary.get('currency', '')}".strip()
                if salary else None,
                "posted_at": _iso(j.get("createdAt")),
            })
    return out


def fetch_ashby(client: httpx.Client, cfg: dict) -> list[dict]:
    out = []
    for slug in cfg.get("companies", []):
        r = client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            params={"includeCompensation": "true"},
        )
        if r.status_code == 404:
            log.warning("ashby board not found: %s", slug)
            continue
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            if j.get("isListed") is False:
                continue
            out.append({
                "source": "ashby",
                "external_id": f"{slug}:{j['id']}",
                "title": j.get("title", ""),
                "company": slug,
                "location": j.get("location"),
                "url": j.get("jobUrl") or j.get("applyUrl"),
                "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                "salary": (j.get("compensation") or {}).get("compensationTierSummary"),
                "posted_at": _iso(j.get("publishedAt")),
            })
    return out


SOURCES = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(sources_cfg: dict) -> tuple[list[dict], list[str]]:
    """Fetch from every enabled source. Returns (jobs, errors); one failing source doesn't stop the rest."""
    jobs, errors = [], []
    headers = {"User-Agent": "job-search-assistant/1.0"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        for name, cfg in (sources_cfg or {}).items():
            if not cfg or not cfg.get("enabled", True) or name not in SOURCES:
                continue
            try:
                got = SOURCES[name](client, cfg)
                log.info("%s: %d jobs", name, len(got))
                jobs.extend(got)
            except Exception as e:  # noqa: BLE001 - surface per-source failures, keep going
                log.exception("source %s failed", name)
                errors.append(f"{name}: {e}")
    return jobs, errors
