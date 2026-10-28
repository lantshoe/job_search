"""fetch -> keyword filter -> LLM score. Run from the web UI or: python -m app.pipeline"""
import copy
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, filters, llm
from .config import POSTED_WITHIN, load_settings
from .sources import fetch_all

log = logging.getLogger(__name__)

_lock = threading.Lock()
state = {"running": False, "stage": "idle", "progress": "", "last_error": None}


def _set(stage: str, progress: str = ""):
    state["stage"], state["progress"] = stage, progress
    log.info("[%s] %s", stage, progress)


def fetch_and_filter(settings: dict) -> dict:
    _set("fetching")
    filter_cfg = settings.get("filter", {})
    jobs, errors = fetch_all(settings.get("sources", {}), filter_cfg)
    new_ids, passed = [], 0
    _set("filtering", f"{len(jobs)} fetched")
    with db.session() as conn:
        for job in jobs:
            job_id = db.upsert_job(conn, job)
            if job_id is None:
                continue  # already seen in an earlier run
            new_ids.append(job_id)
            ok, reason = filters.check(job, filter_cfg)
            db.set_filter_result(conn, job_id, ok, reason)
            passed += ok
    return {"fetched": len(jobs), "new_jobs": len(new_ids), "passed": passed, "errors": errors}


def score_pending(settings: dict) -> int:
    resume = db.active_resume()
    if not resume:
        raise RuntimeError("Upload a resume before scoring.")
    llm_cfg = settings.get("llm", {})
    pending = db.unscored_jobs(resume["id"], llm_cfg.get("max_jobs_per_run", 40))
    if not pending:
        return 0
    system = llm.build_score_system(resume["content"], db.feedback_history())
    max_jd_chars = llm_cfg.get("max_jd_chars", 8000)
    _, model = llm.detect(llm.get_settings())  # fail fast if the Colab link is down

    done, failures = 0, []

    def work(job):
        return job, llm.score_job(job, system, max_jd_chars)

    def record(fut_or_job):
        nonlocal done
        try:
            job, result = fut_or_job
            db.save_score(job["id"], resume["id"], result, model)
            done += 1
        except Exception as e:  # noqa: BLE001
            failures.append(str(e))
        _set("scoring", f"{done}/{len(pending)} scored")

    _set("scoring", f"0/{len(pending)} scored")
    with ThreadPoolExecutor(max_workers=llm_cfg.get("concurrency", 1)) as pool:
        futures = [pool.submit(work, j) for j in pending]
        for fut in as_completed(futures):
            try:
                record(fut.result())
            except Exception as e:  # noqa: BLE001
                failures.append(str(e))
                log.warning("scoring failed: %s", e)
    if failures:
        state["last_error"] = f"{len(failures)} job(s) failed to score, e.g. {failures[0]}"
    return done


def apply_overrides(settings: dict, queries: list[str] | None = None, posted_within: str | None = None) -> dict:
    """Keywords / time range chosen on the web page replace config.yaml for one run."""
    s = copy.deepcopy(settings)
    sources = s.get("sources") or {}
    for cfg in sources.values():
        if cfg and "queries" in cfg:
            if queries:
                cfg["queries"] = list(queries)
            elif posted_within:
                # Drop per-query date_posted so the chosen range applies to every query.
                cfg["queries"] = [q["query"] if isinstance(q, dict) else q for q in cfg["queries"]]
    if posted_within:
        _, days = POSTED_WITHIN[posted_within]
        if sources.get("greenhouse_search"):
            sources["greenhouse_search"]["date_posted"] = None if posted_within == "any" else posted_within
        s.setdefault("filter", {})["max_age_days"] = days
    return s


def default_queries(settings: dict) -> list[str]:
    for name in ("greenhouse_search", "remotive"):
        cfg = (settings.get("sources") or {}).get(name)
        if cfg and cfg.get("enabled", True) and cfg.get("queries"):
            return [q["query"] if isinstance(q, dict) else q for q in cfg["queries"]]
    return []


def run(fetch: bool = True, queries: list[str] | None = None, posted_within: str | None = None) -> dict:
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A pipeline run is already in progress.")
    state.update(running=True, last_error=None)
    run_id = db.start_run()
    summary = {"fetched": 0, "new_jobs": 0, "passed": 0, "scored": 0}
    try:
        settings = apply_overrides(load_settings(), queries, posted_within)
        if fetch:
            f = fetch_and_filter(settings)
            summary.update({k: f[k] for k in ("fetched", "new_jobs", "passed")})
            if f["errors"]:
                state["last_error"] = "; ".join(f["errors"])
        summary["scored"] = score_pending(settings)
        db.finish_run(run_id, **summary, error=state["last_error"])
        _set("done", f"{summary['new_jobs']} new, {summary['passed']} passed filter, {summary['scored']} scored")
        return summary
    except Exception as e:
        state["last_error"] = str(e)
        db.finish_run(run_id, **summary, error=str(e))
        _set("failed", str(e))
        raise
    finally:
        state["running"] = False
        _lock.release()


def rescore_all():
    """Drop scores for undecided jobs so they get re-ranked with the latest feedback."""
    resume = db.active_resume()
    if not resume:
        raise RuntimeError("Upload a resume first.")
    with db.session() as conn:
        conn.execute(
            "DELETE FROM scores WHERE resume_id = ? AND job_id IN (SELECT id FROM jobs WHERE status = 'new')",
            (resume["id"],),
        )
    return run(fetch=False)


if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--no-fetch", action="store_true", help="only score jobs already in the DB")
    p.add_argument("--query", action="append", help="keyword search (repeatable); replaces config.yaml queries")
    p.add_argument("--posted", choices=list(POSTED_WITHIN), help="only jobs posted within this range")
    args = p.parse_args()
    db.init_db()
    print(json.dumps(run(fetch=not args.no_fetch, queries=args.query, posted_within=args.posted), indent=2))
    if state["last_error"]:
        print("warnings:", state["last_error"])
