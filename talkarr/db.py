import json
import sqlite3
import threading
import time

from .config import Config

_local = threading.local()


class JobStatus:
    PENDING = "pending"
    CLASSIFYING = "classifying"
    SYNCING = "syncing"
    CLEANING = "cleaning"
    VALIDATING = "validating"
    VERIFYING = "verifying"
    FLAGGED = "flagged"  # failed validation; in dry-run nothing is blacklisted
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLACKLISTED = "blacklisted"
    SKIPPED = "skipped"


class SubType:
    REGULAR = "regular"
    FORCED = "forced"
    HI = "hi"
    UNKNOWN = "unknown"


def get_db():
    if not hasattr(_local, "db") or _local.db is None:
        _local.db = sqlite3.connect(Config.DATABASE_PATH)
        _local.db.row_factory = sqlite3.Row
        _local.db.execute("PRAGMA journal_mode=WAL")
    return _local.db


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                subtitle_path TEXT NOT NULL,
                media_path TEXT,
                language TEXT,
                sub_type TEXT DEFAULT 'unknown',
                source TEXT DEFAULT 'bazarr',
                status TEXT DEFAULT 'pending',
                sync_offset REAL,
                sync_confidence REAL,
                validation_score REAL,
                validation_notes TEXT,
                actions_taken TEXT,
                error TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_subtitle ON jobs(subtitle_path);
        """)
        _migrate(db)


# Columns added after the first release; added in place on existing databases.
_ADDED_COLUMNS = {
    "verdict": "TEXT",  # forced-subtitle verdict (in_sync, retimed, mislabelled, ...)
    "review": "INTEGER DEFAULT 0",  # verdict is plausible but needs a human look
    "report": "TEXT",  # JSON: reason, proposed correction, evidence details
    "proposal": "TEXT",  # JSON list of fixes (see talkarr/fixes.py)
    "outcome": "TEXT",  # pending | applied | partial | dismissed | nothing | failed
    "applied_at": "REAL",
    "backup": "TEXT",  # JSON: what was backed up/changed, for revert
}


def _migrate(db):
    existing = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
    for column, decl in _ADDED_COLUMNS.items():
        if column not in existing:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_verdict ON jobs(verdict)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_outcome ON jobs(outcome)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
    )
    db.commit()


def update_job_report(job_id, verdict, review, report):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET verdict = ?, review = ?, report = ?, updated_at = ? WHERE id = ?",
            (verdict, 1 if review else 0, json.dumps(report), now, job_id),
        )
        db.commit()


def create_job(job_id, subtitle_path, media_path, language, source="bazarr"):
    now = time.time()
    with get_db() as db:
        db.execute(
            "INSERT INTO jobs (id, subtitle_path, media_path, language, "
            "source, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, subtitle_path, media_path, language, source, JobStatus.PENDING, now, now),
        )
        db.commit()


def get_job(job_id):
    row = get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_jobs_by_status(status):
    rows = (
        get_db()
        .execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at",
            (status,),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def update_job_status(job_id, status, error=None):
    now = time.time()
    with get_db() as db:
        if error:
            db.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, now, job_id),
            )
        else:
            db.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )
        db.commit()


def update_job_classification(job_id, sub_type):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET sub_type = ?, updated_at = ? WHERE id = ?",
            (sub_type, now, job_id),
        )
        db.commit()


def update_job_sync(job_id, offset, confidence):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET sync_offset = ?, sync_confidence = ?, updated_at = ? WHERE id = ?",
            (offset, confidence, now, job_id),
        )
        db.commit()


def update_job_validation(job_id, score, notes):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET validation_score = ?, validation_notes = ?, "
            "updated_at = ? WHERE id = ?",
            (score, json.dumps(notes) if isinstance(notes, (list, dict)) else notes, now, job_id),
        )
        db.commit()


def update_job_actions(job_id, actions):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET actions_taken = ?, updated_at = ? WHERE id = ?",
            (json.dumps(actions), now, job_id),
        )
        db.commit()


def get_recent_jobs(limit=50, verdict=None, review=None):
    clauses, params = [], []
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    if review is not None:
        clauses.append("review = ?")
        params.append(1 if review else 0)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    rows = (
        get_db()
        .execute(f"SELECT * FROM jobs {where}ORDER BY created_at DESC LIMIT ?", (*params, limit))
        .fetchall()
    )
    return [dict(r) for r in rows]


def get_job_by_subtitle(subtitle_path):
    row = (
        get_db()
        .execute(
            "SELECT * FROM jobs WHERE subtitle_path = ? ORDER BY created_at DESC LIMIT 1",
            (subtitle_path,),
        )
        .fetchone()
    )
    return dict(row) if row else None


def get_stats():
    db = get_db()
    rows = db.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status").fetchall()
    stats = {r["status"]: r["count"] for r in rows}
    total = db.execute("SELECT COUNT(*) as count FROM jobs").fetchone()
    stats["total"] = total["count"]
    return stats


def update_job_proposal(job_id, fixes, outcome):
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET proposal = ?, outcome = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fixes), outcome, time.time(), job_id),
        )
        db.commit()


def update_job_applied(job_id, fixes, outcome, backup):
    now = time.time()
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET proposal = ?, outcome = ?, backup = ?, applied_at = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(fixes), outcome, json.dumps(backup), now, now, job_id),
        )
        db.commit()


# Only the most recent job for each subtitle file: re-checks used to add rows, and a
# subtitle should appear once in every list and count.
LATEST_ONLY = (
    "id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY subtitle_path "
    "ORDER BY created_at DESC, rowid DESC) AS rn FROM jobs) WHERE rn = 1)"
)


def get_jobs(outcome=None, verdict=None, sub_type=None, review=None, search=None, limit=200):
    clauses, params = [LATEST_ONLY], []
    for column, value in (("outcome", outcome), ("verdict", verdict), ("sub_type", sub_type)):
        if value:
            values = value if isinstance(value, (list, tuple)) else [value]
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
    if review is not None:
        clauses.append("review = ?")
        params.append(1 if review else 0)
    if search:
        clauses.append("subtitle_path LIKE ?")
        params.append(f"%{search}%")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    rows = (
        get_db()
        .execute(f"SELECT * FROM jobs {where}ORDER BY updated_at DESC LIMIT ?", (*params, limit))
        .fetchall()
    )
    return [dict(r) for r in rows]


def count_by(column):
    rows = (
        get_db()
        .execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM jobs WHERE {LATEST_ONLY} GROUP BY {column}"
        )
        .fetchall()
    )
    return {r["k"]: r["n"] for r in rows}


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )
        db.commit()


# Statuses a job only has while the worker is on it.
IN_PROGRESS = ("classifying", "cleaning", "syncing", "verifying", "validating")


def requeue_interrupted():
    """Jobs left mid-check by a restart go back in the queue. Returns how many."""
    with get_db() as db:
        cur = db.execute(
            f"UPDATE jobs SET status = 'pending', updated_at = ? "
            f"WHERE status IN ({','.join('?' * len(IN_PROGRESS))})",
            (time.time(), *IN_PROGRESS),
        )
        db.commit()
        return cur.rowcount


def next_pending():
    """The next job to check: anything you or Bazarr queued goes before a scan backlog,
    then oldest first."""
    row = (
        get_db()
        .execute(
            "SELECT * FROM jobs WHERE status = 'pending' "
            "ORDER BY (source = 'bulk'), created_at, rowid LIMIT 1"
        )
        .fetchone()
    )
    return dict(row) if row else None


def set_job_source(job_id, source):
    with get_db() as db:
        db.execute("UPDATE jobs SET source = ? WHERE id = ?", (source, job_id))
        db.commit()
