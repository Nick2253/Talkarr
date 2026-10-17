import json

from talkarr import db
from talkarr.db import JobStatus


def test_job_lifecycle():
    db.create_job("j1", "/m/a.srt", "/m/a.mkv", "en")
    job = db.get_job("j1")
    assert job["status"] == JobStatus.PENDING
    assert job["source"] == "bazarr"

    db.update_job_classification("j1", "forced")
    db.update_job_sync("j1", 1.5, 0.9)
    db.update_job_validation("j1", 0.8, {"issues": [], "warnings": ["w"]})
    db.update_job_actions("j1", ["a", "b"])
    db.update_job_status("j1", JobStatus.FAILED, "boom")

    job = db.get_job("j1")
    assert job["sub_type"] == "forced"
    assert job["sync_offset"] == 1.5 and job["sync_confidence"] == 0.9
    assert json.loads(job["validation_notes"]) == {"issues": [], "warnings": ["w"]}
    assert json.loads(job["actions_taken"]) == ["a", "b"]
    assert job["status"] == JobStatus.FAILED and job["error"] == "boom"


def test_unknown_job():
    assert db.get_job("nope") is None


def test_queries_and_stats():
    db.create_job("a", "/m/1.srt", "/m/1.mkv", "en")
    db.create_job("b", "/m/2.srt", "/m/2.mkv", "en", source="bulk")
    db.create_job("c", "/m/1.srt", "/m/1.mkv", "en")
    db.update_job_status("a", JobStatus.COMPLETED)

    assert [j["id"] for j in db.get_jobs_by_status(JobStatus.PENDING)] == ["b", "c"]
    assert db.get_job_by_subtitle("/m/1.srt")["id"] in {"a", "c"}
    assert db.get_job_by_subtitle("/m/none.srt") is None
    assert len(db.get_recent_jobs(limit=2)) == 2
    assert db.get_stats() == {"pending": 2, "completed": 1, "total": 3}


def test_string_validation_notes_stored_verbatim():
    db.create_job("j", "/m/a.srt", None, "")
    db.update_job_validation("j", 1.0, "plain")
    assert db.get_job("j")["validation_notes"] == "plain"


def test_init_is_idempotent():
    db.init_db()
    db.init_db()
