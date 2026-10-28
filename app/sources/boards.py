"""Company job-board platforms (ATS): Greenhouse, Lever, Ashby, Workable.

These have no search API, only a public per-company endpoint, so they're driven by a
company list you maintain in companies/<platform>.txt (one company per line).

To add a platform: write a `_fetch_<name>(client, slug)` that returns normalized jobs
(raise NotFound for an unknown company), add a Platform entry to PLATFORMS, create
companies/<name>.txt, and enable it under `sources:` in config.yaml.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import httpx

from ..config import ROOT
from .common import NotFound, html_to_text, iso

log = logging.getLogger(__name__)


def _get_json(client: httpx.Client, url: str, **params):
    r = client.get(url, params=params or None)
    if r.status_code == 404:
        raise NotFound(url)
    r.raise_for_status()
    return r.json()


def _join(*parts) -> str | None:
    return ", ".join(p for p in parts if p) or None


# ---------------------------------------------------------------- platforms

def _fetch_greenhouse(client: httpx.Client, slug: str) -> list[dict]:
    data = _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", content="true")
    return [{
        "source": "greenhouse",
        "external_id": f"{slug}:{j['id']}",
        "title": j.get("title", ""),
        "company": j.get("company_name") or slug,
        "location": (j.get("location") or {}).get("name"),
        "url": j.get("absolute_url"),
        "description": html_to_text(j.get("content")),
        "salary": None,
        "posted_at": iso(j.get("first_published") or j.get("updated_at")),
    } for j in data.get("jobs", [])]


def _fetch_lever(client: httpx.Client, slug: str) -> list[dict]:
    try:
        data = _get_json(client, f"https://api.lever.co/v0/postings/{slug}", mode="json")
    except NotFound:
        # Companies hosted in Lever's EU region live on a separate API host.
        data = _get_json(client, f"https://api.eu.lever.co/v0/postings/{slug}", mode="json")
    out = []
    for j in data:
        sections = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists", []):
            sections.append(f"**{lst.get('text', '')}**\n{html_to_text(lst.get('content'))}")
        sections.append(j.get("additionalPlain") or "")
        salary = j.get("salaryRange") or {}
        location = (j.get("categories") or {}).get("location")
        if j.get("workplaceType") and j["workplaceType"] != "unspecified":
            location = f"{location or ''} ({j['workplaceType']})".strip()
        out.append({
            "source": "lever",
            "external_id": f"{slug}:{j['id']}",
            "title": j.get("text", ""),
            "company": slug,
            "location": location,
            "url": j.get("hostedUrl") or j.get("applyUrl"),
            "description": "\n\n".join(s for s in sections if s).strip(),
            "salary": f"{salary.get('min')}-{salary.get('max')} {salary.get('currency', '')}".strip()
            if salary else None,
            "posted_at": iso(j.get("createdAt")),
        })
    return out


def _fetch_ashby(client: httpx.Client, slug: str) -> list[dict]:
    data = _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}", includeCompensation="true")
    if "jobs" not in data:
        raise NotFound(slug)
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        location = j.get("location")
        if j.get("workplaceType"):
            location = f"{location or ''} ({j['workplaceType']})".strip()
        elif j.get("isRemote"):
            location = f"{location or ''} (Remote)".strip()
        out.append({
            "source": "ashby",
            "external_id": f"{slug}:{j['id']}",
            "title": j.get("title", ""),
            "company": slug,
            "location": location,
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "description": html_to_text(j.get("descriptionHtml")) or j.get("descriptionPlain") or "",
            "salary": (j.get("compensation") or {}).get("compensationTierSummary"),
            "posted_at": iso(j.get("publishedAt")),
        })
    return out


def _fetch_workable(client: httpx.Client, slug: str) -> list[dict]:
    data = _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}", details="true")
    company = data.get("name") or slug
    out = []
    for j in data.get("jobs", []):
        locs = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
        location = " | ".join(filter(None, (_join(l.get("city"), l.get("region"), l.get("country")) for l in locs)))
        if str(j.get("telecommuting")).lower() == "true":
            location = f"{location} (Remote)".strip()
        out.append({
            "source": "workable",
            "external_id": f"{slug}:{j['shortcode']}",
            "title": j.get("title", ""),
            "company": company,
            "location": location or None,
            "url": j.get("url") or j.get("shortlink"),
            "description": html_to_text(j.get("description")),
            "salary": None,
            "posted_at": iso(j.get("published_on") or j.get("created_at")),
        })
    return out


@dataclass
class Platform:
    fetch: Callable[[httpx.Client, str], list[dict]]
    # Patterns that pull the company slug out of a pasted careers/job URL.
    url_patterns: tuple[str, ...]
    example: str


PLATFORMS: dict[str, Platform] = {
    "greenhouse": Platform(
        _fetch_greenhouse,
        (r"greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?([\w.-]+)",),
        "https://job-boards.greenhouse.io/stripe",
    ),
    "lever": Platform(
        _fetch_lever,
        (r"lever\.co/(?:v0/postings/)?([\w.-]+)",),
        "https://jobs.lever.co/palantir",
    ),
    "ashby": Platform(
        _fetch_ashby,
        (r"ashbyhq\.com/(?:posting-api/job-board/)?([\w.%-]+)",),
        "https://jobs.ashbyhq.com/notion",
    ),
    "workable": Platform(
        _fetch_workable,
        (r"apply\.workable\.com/(?:api/v1/widget/accounts/)?(?!j/)([\w.-]+)",
         r"(?:^|//)(?!apply\.|www\.)([\w-]+)\.workable\.com"),
        "https://apply.workable.com/huggingface",
    ),
}


# ---------------------------------------------------------------- company lists

def parse_company(line: str, platform: str) -> str | None:
    """'stripe', 'stripe  # Stripe Inc', or a careers URL -> 'stripe'. Blank/comment lines -> None."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    if "/" in line or re.search(r"\.(com|co|io)\b", line):
        for pattern in PLATFORMS[platform].url_patterns:
            if m := re.search(pattern, line):
                return m.group(1)
        log.warning("%s: can't find a company in '%s' (expected e.g. %s)", platform, line, PLATFORMS[platform].example)
        return None
    return line.split()[0]


def load_companies(platform: str, cfg: dict) -> list[str]:
    """Companies from companies/<platform>.txt (or `companies_file`) plus any inline `companies:`."""
    lines = list(cfg.get("companies") or [])
    path = ROOT / cfg.get("companies_file", f"companies/{platform}.txt")
    if path.exists():
        lines += path.read_text().splitlines()
    seen, out = set(), []
    for line in lines:
        slug = parse_company(str(line), platform)
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            out.append(slug)
    return out


def fetch_platform(client: httpx.Client, platform: str, cfg: dict) -> tuple[list[dict], list[str]]:
    """Fetch every company on one platform in parallel. A failing company never stops the rest."""
    companies = load_companies(platform, cfg)
    fetch = PLATFORMS[platform].fetch
    jobs, not_found, failed = [], [], []

    def one(slug):
        try:
            return slug, fetch(client, slug), None
        except NotFound:
            return slug, [], "not_found"
        except Exception as e:  # noqa: BLE001 - reported per company
            return slug, [], str(e)

    with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 8)) as pool:
        for slug, got, err in pool.map(one, companies):
            if err == "not_found":
                not_found.append(slug)
            elif err:
                failed.append(f"{slug} ({err[:80]})")
            else:
                for job in got:
                    job["title"] = (job["title"] or "").strip()
                jobs.extend(got)

    errors = []
    list_file = cfg.get("companies_file", f"companies/{platform}.txt")
    if not_found:
        errors.append(f"{len(not_found)} not found on {platform}, check {list_file}: {', '.join(not_found[:10])}"
                      + (" …" if len(not_found) > 10 else ""))
    if failed:
        errors.append(f"{len(failed)} failed: {'; '.join(failed[:3])}" + (" …" if len(failed) > 3 else ""))
    log.info("%s: %d jobs from %d companies", platform, len(jobs), len(companies) - len(not_found) - len(failed))
    return jobs, errors
