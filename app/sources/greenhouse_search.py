"""MyGreenhouse keyword search.

my.greenhouse.io/jobs/search is the (login-only) Inertia.js endpoint behind the
MyGreenhouse job-seeker site. With the X-Inertia header it returns JSON
{component, props, version} instead of HTML. Search results have no description,
so each new job's full text comes from the public boards API.
"""
import html
import json
import os
import re
import time

import httpx

from .. import db, filters
from .common import SessionExpired, html_to_text, iso

GH_SEARCH_URL = "https://my.greenhouse.io/jobs/search"
EXPIRED_MSG = "MyGreenhouse cookie expired. Copy a fresh one from your browser into GREENHOUSE_COOKIE in .env"


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
        "posted_at": iso(raw.get("firstPublished")),
        "_token": token,
        "_job_id": job_id,
    }


class GreenhouseSearch:
    def __init__(self, cookie: str, user_agent: str, delay: float):
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
        db.set_setting("greenhouse_inertia_version", self.version)

    @staticmethod
    def _check_login(r: httpx.Response):
        if r.status_code in (301, 302, 303, 401) or "/users/sign_in" in r.headers.get("location", ""):
            raise SessionExpired(EXPIRED_MSG)

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
                raise SessionExpired(EXPIRED_MSG)
            if d.get("version") and d["version"] != self.version:
                self.version = d["version"]
                db.set_setting("greenhouse_inertia_version", self.version)
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
                        job["posted_at"] = job["posted_at"] or iso(d.get("first_published") or d.get("updated_at"))
                out.append(job)
    finally:
        gh.close()
    return out
