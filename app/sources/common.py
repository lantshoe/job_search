"""Helpers shared by all job sources.

Every source returns normalized job dicts:
    {source, external_id, title, company, location, url, description, salary, posted_at}
"""
from datetime import datetime, timezone

from ..jd_parse import markdown as html_to_text  # noqa: F401 - re-exported for the source modules


class SessionExpired(RuntimeError):
    """A source that needs a login has lost it; the user has to refresh credentials."""


class NotFound(RuntimeError):
    """A company slug doesn't exist on that platform (usually a typo in the company list)."""


def iso(value) -> str | None:
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
