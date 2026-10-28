"""Job source adapters. Each returns a list of normalized job dicts:

    {source, external_id, title, company, location, url, description, salary, posted_at}

To add a source, write a `fetch_<name>(client, cfg)` function and register it in SOURCES.
"""
import html
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .jd_parse import markdown as html_to_text

log = logging.getLogger(__name__)


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
        q = q["query"] if isinstance(q, dict) else q
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


# ---------------------------------------------------------------- MyGreenhouse keyword search
#
# my.greenhouse.io/jobs/search is the (login-only) Inertia.js endpoint behind the
# MyGreenhouse job-seeker site. With the X-Inertia header it returns JSON
# {component, props, version} instead of HTML. Search results have no description,
# so each new job's full text comes from the public boards API.

GH_SEARCH_URL = "https://my.greenhouse.io/jobs/search"


class SessionExpired(RuntimeError):
    pass


def _gh_listing(raw: dict) -> dict:
    """One MyGreenhouse search result -> normalized job (description filled in later)."""
    token, job_id = raw.get("urlToken") or "", raw["id"]
    location = " | ".join(raw.get("locations") or [])
    if raw.get("workType"):
        location = f"{location} ({raw['workType']})" if location else raw["workType"]
    return {
        "source": "greenhouse",
        "external_id": f"{token}:{job_id}",
        "title": (raw.get("title") or "").strip(),
        "company": (raw.get("companyName") or "").strip() or token,
        "location": location or None,
        "url": raw.get("publicUrl") or f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
        "description": "",
        "salary": raw.get("payRanges") or None,
        "posted_at": _iso(raw.get("firstPublished")),
        "_token": token,
        "_job_id": job_id,
    }


class GreenhouseSearch:
    def __init__(self, cookie: str, user_agent: str, delay: float):
        from . import db

        self.db = db
        self.delay = delay
        self.version = db.get_setting("greenhouse_inertia_version") or ""
        self.client = httpx.Client(timeout=30, follow_redirects=False, headers={
            "Accept": "text/html, application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie,
            "User-Agent": user_agent,
            "Referer": "https://my.greenhouse.io/jobs",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
        })

    def close(self):
        self.client.close()

    def _refresh_version(self, params: dict):
        """On a 409 the server's asset version changed. A plain (non-Inertia) page load
        embeds the page JSON, including the new version, in a data-page attribute."""
        r = self.client.get(GH_SEARCH_URL, params=params)
        time.sleep(self.delay)
        self._check_login(r)
        m = re.search(r'data-page="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("Greenhouse returned 409 and no page data to read the new version from")
        page = json.loads(html.unescape(m.group(1)))
        self.version = page.get("version") or ""
        self.db.set_setting("greenhouse_inertia_version", self.version)

    @staticmethod
    def _check_login(r: httpx.Response):
        if r.status_code in (301, 302, 303, 401) or "/users/sign_in" in r.headers.get("location", ""):
            raise SessionExpired("MyGreenhouse cookie expired. Copy a fresh one from your browser into GREENHOUSE_COOKIE in .env")

    def get(self, params: dict) -> dict:
        for _ in range(2):
            r = self.client.get(GH_SEARCH_URL, params=params,
                                headers={"X-Inertia": "true", "X-Inertia-Version": self.version})
            time.sleep(self.delay)
            self._check_login(r)
            if r.status_code == 409:
                self._refresh_version(params)
                continue
            r.raise_for_status()
            d = r.json()
            if d.get("component") == "login":
                raise SessionExpired("MyGreenhouse cookie expired. Copy a fresh one from your browser into GREENHOUSE_COOKIE in .env")
            if d.get("version") and d["version"] != self.version:
                self.version = d["version"]
                self.db.set_setting("greenhouse_inertia_version", self.version)
            return d.get("props") or {}
        raise RuntimeError("Greenhouse search still returned 409 after refreshing the version")

    def search(self, query: str, extra: dict, max_pages: int):
        for page in range(1, max_pages + 1):
            props = self.get({"query": query, "page": page, **extra})
            posts = props.get("jobPosts") or []
            if not posts:
                return
            yield from posts
            if not props.get("moreResultsAvailable"):
                return


def fetch_greenhouse_search(client: httpx.Client, cfg: dict, filter_cfg: dict | None = None) -> list[dict]:
    import os

    from . import db, filters

    cookie = os.getenv("GREENHOUSE_COOKIE", "").strip()
    if not cookie:
        raise SessionExpired("GREENHOUSE_COOKIE is not set in .env")
    user_agent = os.getenv("GREENHOUSE_USER_AGENT") or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    delay = float(cfg.get("delay_seconds", 1.0))
    common = {k: v for k, v in (cfg.get("params") or {}).items() if v not in (None, "")}
    known = db.known_external_ids("greenhouse")

    gh = GreenhouseSearch(cookie, user_agent, delay)
    out, seen = [], set()
    try:
        for q in cfg.get("queries") or []:
            query, date_posted = (q["query"], q.get("date_posted", cfg.get("date_posted"))) \
                if isinstance(q, dict) else (q, cfg.get("date_posted"))
            extra = {**common, **({"date_posted": date_posted} if date_posted else {})}
            for raw in gh.search(query, extra, cfg.get("max_pages", 5)):
                job = _gh_listing(raw)
                token, job_id = job.pop("_token"), job.pop("_job_id")
                if not token or job["external_id"] in seen or job["external_id"] in known:
                    continue
                seen.add(job["external_id"])

                # Only spend a detail request on jobs whose title survives the keyword filter.
                if filter_cfg is None or filters.check_title(job["title"], filter_cfg)[0]:
                    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
                    time.sleep(delay)
                    if r.status_code == 200:
                        d = r.json()
                        job["description"] = html_to_text(d.get("content"))
                        job["location"] = job["location"] or (d.get("location") or {}).get("name")
                        job["posted_at"] = job["posted_at"] or _iso(d.get("first_published") or d.get("updated_at"))
                out.append(job)
    finally:
        gh.close()
    return out


SOURCES = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "greenhouse_search": fetch_greenhouse_search,
}


def fetch_all(sources_cfg: dict, filter_cfg: dict | None = None) -> tuple[list[dict], list[str]]:
    """Fetch from every enabled source. Returns (jobs, errors); one failing source doesn't stop the rest."""
    jobs, errors = [], []
    headers = {"User-Agent": "job-search-assistant/1.0"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        for name, cfg in (sources_cfg or {}).items():
            if not cfg or not cfg.get("enabled", True) or name not in SOURCES:
                continue
            try:
                if name == "greenhouse_search":
                    got = fetch_greenhouse_search(client, cfg, filter_cfg)
                else:
                    got = SOURCES[name](client, cfg)
                log.info("%s: %d jobs", name, len(got))
                jobs.extend(got)
            except SessionExpired as e:
                log.warning("%s: %s", name, e)
                errors.append(f"{name}: {e}")
            except Exception as e:  # noqa: BLE001 - surface per-source failures, keep going
                log.exception("source %s failed", name)
                errors.append(f"{name}: {e}")
    return jobs, errors
