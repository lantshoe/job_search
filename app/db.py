import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
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
    content     TEXT NOT NULL,
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


# ---------------------------------------------------------------- resumes

def add_resume(filename: str, content: str) -> int:
    with session() as conn:
        conn.execute("UPDATE resumes SET is_active = 0")
        cur = conn.execute(
            "INSERT INTO resumes (filename, content, is_active, created_at) VALUES (?, ?, 1, ?)",
            (filename, content, now()),
        )
        return cur.lastrowid


def active_resume() -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE is_active = 1").fetchone()
        return dict(row) if row else None


def list_resumes() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, filename, is_active, created_at, length(content) AS chars FROM resumes ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def activate_resume(resume_id: int):
    with session() as conn:
        conn.execute("UPDATE resumes SET is_active = (id = ?)", (resume_id,))


# ---------------------------------------------------------------- jobs

def upsert_job(conn: sqlite3.Connection, job: dict) -> int | None:
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


def set_filter_result(conn, job_id: int, passed: bool, reason: str | None):
    conn.execute(
        "UPDATE jobs SET filter_status = ?, filter_reason = ? WHERE id = ?",
        ("passed" if passed else "dropped", reason, job_id),
    )


def unscored_jobs(resume_id: int, limit: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """SELECT j.* FROM jobs j
               LEFT JOIN scores s ON s.job_id = j.id AND s.resume_id = ?
               WHERE j.filter_status = 'passed' AND s.job_id IS NULL AND j.status = 'new'
               ORDER BY j.posted_at DESC, j.id DESC LIMIT ?""",
            (resume_id, limit),
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


def list_jobs(resume_id: int | None) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """SELECT j.id, j.source, j.title, j.company, j.location, j.url, j.salary, j.posted_at,
                      j.fetched_at, j.filter_status, j.filter_reason, j.status, j.decline_reason,
                      j.decline_note, j.decided_at,
                      s.score, s.verdict, s.summary, s.matched, s.gaps, s.sponsorship, s.years_required,
                      (SELECT COUNT(*) FROM tailored_resumes t WHERE t.job_id = j.id) AS tailored_count
               FROM jobs j
               LEFT JOIN scores s ON s.job_id = j.id AND s.resume_id = ?
               ORDER BY s.score IS NULL, s.score DESC, j.fetched_at DESC""",
            (resume_id or -1,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["matched"] = json.loads(d["matched"]) if d["matched"] else []
        d["gaps"] = json.loads(d["gaps"]) if d["gaps"] else []
        out.append(d)
    return out


def get_job(job_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def set_decision(job_id: int, status: str, reason: str | None = None, note: str | None = None):
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

def save_tailored(job_id: int, resume_id: int, content: str) -> int:
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO tailored_resumes (job_id, resume_id, content, created_at) VALUES (?, ?, ?, ?)",
            (job_id, resume_id, content, now()),
        )
        return cur.lastrowid


def latest_tailored(job_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM tailored_resumes WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        return dict(row) if row else None


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
