import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,   -- no longer used
    included    INTEGER NOT NULL DEFAULT 1,   -- sent to the LLM
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT,
    location       TEXT,
    url            TEXT,
    description    TEXT,
    salary         TEXT,
    posted_at      TEXT,
    fetched_at     TEXT NOT NULL,
    -- rough keyword filter: 'passed' | 'dropped'
    filter_status  TEXT,
    filter_reason  TEXT,
    -- user decision: 'new' | 'applied' | 'not_interested'
    status         TEXT NOT NULL DEFAULT 'new',
    decline_reason TEXT,
    decline_note   TEXT,
    decided_at     TEXT,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS scores (
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_id     INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    score         INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    summary       TEXT,
    matched       TEXT,   -- JSON list of strings
    gaps          TEXT,   -- JSON list of {category, detail, severity}
    sponsorship   TEXT,
    years_required INTEGER,
    model         TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (job_id, resume_id)
);

CREATE TABLE IF NOT EXISTS tailored_resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_id   INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,   -- Markdown
    data        TEXT,            -- JSON: {"resume": structured content, "fit": page-fit result}
    created_at  TEXT NOT NULL
);

-- Answers from you that the resumes do not show ("I do have MySQL"). Used for every job.
CREATE TABLE IF NOT EXISTS candidate_facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    topic_key   TEXT NOT NULL UNIQUE,   -- lowercased topic
    kind        TEXT NOT NULL,          -- skill / experience
    has         INTEGER NOT NULL,       -- 1 = you have it, 0 = you do not
    detail      TEXT,
    job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL
);

-- The conversation with the LLM about one job's resume.
CREATE TABLE IF NOT EXISTS tailor_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,          -- user / assistant
    kind        TEXT NOT NULL,          -- review / questions / answers / written / chat
    content     TEXT NOT NULL,
    data        TEXT,                   -- JSON details for the page
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL   -- JSON
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER DEFAULT 0,
    new_jobs    INTEGER DEFAULT 0,
    passed      INTEGER DEFAULT 0,
    scored      INTEGER DEFAULT 0,
    error       TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with session() as conn:
        conn.executescript(SCHEMA)
        # Databases created before the PDF/Word export lack this column.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tailored_resumes)")}
        if "data" not in cols:
            conn.execute("ALTER TABLE tailored_resumes ADD COLUMN data TEXT")
        # Older databases only used the newest upload; now every version is included by default.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resumes)")}
        if "included" not in cols:
            conn.execute("ALTER TABLE resumes ADD COLUMN included INTEGER NOT NULL DEFAULT 1")


# ---------------------------------------------------------------- resumes

def add_resume(filename: str, content: str) -> int:
    """Every upload is kept. Re-uploading identical text just re-includes the existing copy."""
    with session() as conn:
        row = conn.execute("SELECT id FROM resumes WHERE content = ? ORDER BY id DESC LIMIT 1", (content,)).fetchone()
        if row:
            conn.execute("UPDATE resumes SET included = 1 WHERE id = ?", (row["id"],))
            return row["id"]
        cur = conn.execute(
            "INSERT INTO resumes (filename, content, is_active, included, created_at) VALUES (?, ?, 0, 1, ?)",
            (filename, content, now()),
        )
        return cur.lastrowid


def included_resumes() -> list[dict]:
    """The versions sent to the LLM, newest first, skipping exact duplicates."""
    with session() as conn:
        rows = conn.execute("SELECT * FROM resumes WHERE included = 1 ORDER BY id DESC").fetchall()
    seen, out = set(), []
    for r in rows:
        if r["content"] not in seen:
            seen.add(r["content"])
            out.append(dict(r))
    return out


def list_resumes() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, filename, included, created_at, length(content) AS chars FROM resumes ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_resume(resume_id: int) -> Optional[dict]:
    with session() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        return dict(row) if row else None


def set_resume_included(resume_id: int, included: bool):
    with session() as conn:
        conn.execute("UPDATE resumes SET included = ? WHERE id = ?", (int(included), resume_id))


# ---------------------------------------------------------------- facts you confirmed

def upsert_fact(topic: str, kind: str, has: bool, detail: str = "", job_id: Optional[int] = None):
    """One answer per topic; a newer answer replaces the older one."""
    with session() as conn:
        conn.execute(
            """INSERT INTO candidate_facts (topic, topic_key, kind, has, detail, job_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(topic_key) DO UPDATE SET topic = excluded.topic, kind = excluded.kind, has = excluded.has,
                 detail = excluded.detail, job_id = excluded.job_id, created_at = excluded.created_at""",
            (topic.strip(), topic.strip().lower(), kind, int(has), detail.strip(), job_id, now()),
        )


def list_facts() -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM candidate_facts ORDER BY topic_key").fetchall()
        return [dict(r) for r in rows]


def delete_fact(fact_id: int):
    with session() as conn:
        conn.execute("DELETE FROM candidate_facts WHERE id = ?", (fact_id,))


# ---------------------------------------------------------------- jobs

def upsert_job(conn: sqlite3.Connection, job: dict) -> Optional[int]:
    """Insert a job if unseen. Returns the new row id, or None if it already existed."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs
           (source, external_id, title, company, location, url, description, salary, posted_at, fetched_at)
           VALUES (:source, :external_id, :title, :company, :location, :url, :description, :salary, :posted_at, :fetched_at)""",
        {**job, "fetched_at": now()},
    )
    return cur.lastrowid if cur.rowcount else None


def known_external_ids(source: str) -> set[str]:
    with session() as conn:
        return {r[0] for r in conn.execute("SELECT external_id FROM jobs WHERE source = ?", (source,))}


def set_filter_result(conn, job_id: int, passed: bool, reason: Optional[str]):
    conn.execute(
        "UPDATE jobs SET filter_status = ?, filter_reason = ? WHERE id = ?",
        ("passed" if passed else "dropped", reason, job_id),
    )


def unscored_jobs(limit: int = 40) -> list[dict]:
    """Jobs with no score at all; a score from an older resume set still counts."""
    with session() as conn:
        rows = conn.execute(
            """SELECT j.* FROM jobs j
               WHERE j.filter_status = 'passed' AND j.status = 'new'
                 AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_id = j.id)
               ORDER BY j.posted_at DESC, j.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def jobs_to_rerank(limit: int = 40) -> list[dict]:
    """Undecided jobs, unscored first and then the longest since their last score."""
    with session() as conn:
        rows = conn.execute(
            """SELECT j.* FROM jobs j
               WHERE j.filter_status = 'passed' AND j.status = 'new'
               ORDER BY (SELECT MAX(s.created_at) FROM scores s WHERE s.job_id = j.id) IS NOT NULL,
                        (SELECT MAX(s.created_at) FROM scores s WHERE s.job_id = j.id),
                        j.posted_at DESC, j.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_score(job_id: int, resume_id: int, result: dict, model: str):
    with session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scores
               (job_id, resume_id, score, verdict, summary, matched, gaps, sponsorship, years_required, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, resume_id, result["score"], result["verdict"], result["summary"],
                json.dumps(result["matched_skills"]), json.dumps(result["gaps"]),
                result["sponsorship"], result.get("years_required"), model, now(),
            ),
        )


def list_jobs() -> list[dict]:
    """Each job with its most recent score, whichever resume set produced it."""
    with session() as conn:
        rows = conn.execute(
            """SELECT j.id, j.source, j.title, j.company, j.location, j.url, j.salary, j.posted_at,
                      j.fetched_at, j.filter_status, j.filter_reason, j.status, j.decline_reason,
                      j.decline_note, j.decided_at,
                      s.score, s.verdict, s.summary, s.matched, s.gaps, s.sponsorship, s.years_required,
                      (SELECT COUNT(*) FROM tailored_resumes t WHERE t.job_id = j.id) AS tailored_count
               FROM jobs j
               LEFT JOIN scores s ON s.rowid = (
                   SELECT x.rowid FROM scores x WHERE x.job_id = j.id
                   ORDER BY x.created_at DESC, x.rowid DESC LIMIT 1)
               ORDER BY s.score IS NULL, s.score DESC, j.fetched_at DESC"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["matched"] = json.loads(d["matched"]) if d["matched"] else []
        d["gaps"] = json.loads(d["gaps"]) if d["gaps"] else []
        out.append(d)
    return out


def get_job(job_id: int) -> Optional[dict]:
    with session() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def set_decision(job_id: int, status: str, reason: Optional[str] = None, note: Optional[str] = None):
    with session() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, decline_reason = ?, decline_note = ?, decided_at = ? WHERE id = ?",
            (status, reason, note, None if status == "new" else now(), job_id),
        )


def feedback_history(limit: int = 40) -> list[dict]:
    """Recent user decisions, used to teach the LLM the user's preferences."""
    with session() as conn:
        rows = conn.execute(
            """SELECT title, company, location, status, decline_reason, decline_note
               FROM jobs WHERE status IN ('applied', 'not_interested')
               ORDER BY decided_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- tailored resumes

def add_message(job_id: int, role: str, kind: str, content: str, data: Optional[dict] = None):
    with session() as conn:
        conn.execute(
            "INSERT INTO tailor_messages (job_id, role, kind, content, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, role, kind, content, json.dumps(data) if data else None, now()),
        )


def list_messages(job_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM tailor_messages WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]


def save_tailored(job_id: int, resume_id: int, content: str, data: Optional[dict] = None) -> int:
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO tailored_resumes (job_id, resume_id, content, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, resume_id, content, json.dumps(data) if data else None, now()),
        )
        return cur.lastrowid


def update_tailored_data(tailored_id: int, data: dict):
    with session() as conn:
        conn.execute("UPDATE tailored_resumes SET data = ? WHERE id = ?", (json.dumps(data), tailored_id))


def latest_tailored(job_id: int) -> Optional[dict]:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM tailored_resumes WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        if not row:
            return None
        t = dict(row)
        t["data"] = json.loads(t["data"]) if t["data"] else None
        return t


# ---------------------------------------------------------------- settings

def get_setting(key: str):
    with session() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None


def set_setting(key: str, value):
    with session() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# ---------------------------------------------------------------- runs

def start_run() -> int:
    with session() as conn:
        return conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid


def finish_run(run_id: int, **fields):
    fields["finished_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with session() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def last_runs(n: int = 5) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,))]
