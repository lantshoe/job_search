"""Job sources, by kind:

- aggregators.py        boards that cover many companies (Remotive, Arbeitnow)
- boards.py             per-company ATS platforms driven by companies/<platform>.txt
                        (Greenhouse, Lever, Ashby, Workable)
- greenhouse_search.py  logged-in keyword search on my.greenhouse.io

Each source is enabled and configured under `sources:` in config.yaml.
"""
import logging

import httpx

from .aggregators import AGGREGATORS
from .boards import PLATFORMS, fetch_platform
from .common import SessionExpired
from .greenhouse_search import fetch_greenhouse_search

log = logging.getLogger(__name__)

ALL_SOURCES = [*AGGREGATORS, *PLATFORMS, "greenhouse_search"]


def fetch_all(sources_cfg: dict, filter_cfg: dict | None = None) -> tuple[list[dict], list[str]]:
    """Fetch from every enabled source. Returns (jobs, errors); one failing source doesn't stop the rest."""
    jobs, errors = [], []
    headers = {"User-Agent": "job-search-assistant/1.0"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        for name, cfg in (sources_cfg or {}).items():
            if not cfg or not cfg.get("enabled", True):
                continue
            if name not in ALL_SOURCES:
                errors.append(f"{name}: unknown source in config.yaml (known: {', '.join(ALL_SOURCES)})")
                continue
            try:
                if name in PLATFORMS:
                    got, errs = fetch_platform(client, name, cfg)
                    errors += [f"{name}: {e}" for e in errs]
                elif name == "greenhouse_search":
                    got = fetch_greenhouse_search(client, cfg, filter_cfg)
                else:
                    got = AGGREGATORS[name](client, cfg)
                log.info("%s: %d jobs", name, len(got))
                jobs.extend(got)
            except SessionExpired as e:
                log.warning("%s: %s", name, e)
                errors.append(f"{name}: {e}")
            except Exception as e:  # noqa: BLE001 - surface per-source failures, keep going
                log.exception("source %s failed", name)
                errors.append(f"{name}: {e}")
    return jobs, errors
