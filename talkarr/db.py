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


def get_recent_jobs(limit=50):
    rows = (
        get_db().execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
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
