"""Job boards that aggregate many companies and need no company list."""
import httpx

from .common import html_to_text, iso


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
                "posted_at": iso(j.get("publication_date")),
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
                "posted_at": iso(j.get("created_at")),
            })
    return out


AGGREGATORS = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
}
