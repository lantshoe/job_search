"""fetch -> keyword filter -> LLM score. Run from the web UI or: python -m app.pipeline"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, filters, llm
from .config import load_settings
from .sources import fetch_all

log = logging.getLogger(__name__)

_lock = threading.Lock()
state = {"running": False, "stage": "idle", "progress": "", "last_error": None}


def _set(stage: str, progress: str = ""):
    state["stage"], state["progress"] = stage, progress
    log.info("[%s] %s", stage, progress)


def fetch_and_filter(settings: dict) -> dict:
    _set("fetching")
    jobs, errors = fetch_all(settings.get("sources", {}))
    filter_cfg = settings.get("filter", {})
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


def run(fetch: bool = True) -> dict:
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A pipeline run is already in progress.")
    state.update(running=True, last_error=None)
    run_id = db.start_run()
    summary = {"fetched": 0, "new_jobs": 0, "passed": 0, "scored": 0}
    try:
        settings = load_settings()
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
    args = p.parse_args()
    db.init_db()
    print(json.dumps(run(fetch=not args.no_fetch), indent=2))
    if state["last_error"]:
        print("warnings:", state["last_error"])
