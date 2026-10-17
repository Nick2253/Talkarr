import json
from types import SimpleNamespace

import pytest

from talkarr import db, worker
from talkarr.config import Config
from talkarr.db import JobStatus
from tests.conftest import make_srt


@pytest.fixture
def fake_sync(monkeypatch):
    calls = []

    def install(**kwargs):
        defaults = dict(success=True, skipped=False, offset=1.25, confidence=0.9, error=None)
        defaults.update(kwargs)

        def sync_subtitle(sub, media, sub_type):
            calls.append((sub, media, sub_type))
            return SimpleNamespace(**defaults)

        monkeypatch.setattr(worker, "sync_subtitle", sync_subtitle)
        return calls

    return install


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "send_discord_notification", sent.append)
    return sent


def queue(tmp_path, name, cues, job_id="j1"):
    sub = tmp_path / name
    sub.write_text(make_srt([f"line {i}" for i in range(cues)]))
    db.create_job(job_id, str(sub), str(tmp_path / "Movie.mkv"), "en")
    return job_id


def test_regular_sub_is_synced_and_completed(tmp_path, fake_sync, notifications):
    calls = fake_sync()
    job_id = queue(tmp_path, "Movie.en.srt", 300)

    worker.process_pending_jobs()

    job = db.get_job(job_id)
    assert job["status"] == JobStatus.COMPLETED
    assert job["sub_type"] == "regular"
    assert job["sync_offset"] == 1.25
    assert "synced: offset=1.25s" in json.loads(job["actions_taken"])
    assert len(calls) == 1
    assert notifications == []


def test_forced_sub_sync_skip_is_recorded(tmp_path, fake_sync, notifications):
    fake_sync(skipped=True, offset=None, confidence=None)
    job_id = queue(tmp_path, "Movie.en.forced.srt", 20)

    worker.process_pending_jobs()

    job = db.get_job(job_id)
    assert job["status"] == JobStatus.COMPLETED
    assert "sync: skipped (forced sub)" in json.loads(job["actions_taken"])


def test_mislabelled_forced_sub_is_blacklisted_and_notified(tmp_path, fake_sync, notifications):
    fake_sync(skipped=True)
    job_id = queue(tmp_path, "Movie.en.forced.srt", 300)

    worker.process_pending_jobs()

    assert db.get_job(job_id)["status"] == JobStatus.BLACKLISTED
    assert len(notifications) == 1
    assert "Movie.en.forced.srt" in notifications[0]


def test_sync_failure_is_recorded_but_job_continues(tmp_path, fake_sync, notifications):
    fake_sync(success=False, error="no audio")
    job_id = queue(tmp_path, "Movie.en.srt", 300)

    worker.process_pending_jobs()

    job = db.get_job(job_id)
    assert job["status"] == JobStatus.COMPLETED
    assert "sync failed: no audio" in json.loads(job["actions_taken"])


def test_disabled_stages_are_skipped(tmp_path, fake_sync, notifications, monkeypatch):
    calls = fake_sync()
    monkeypatch.setattr(Config, "SYNC_ENABLED", False)
    monkeypatch.setattr(Config, "CLEAN_ENABLED", False)
    job_id = queue(tmp_path, "Movie.en.srt", 300)

    worker.process_pending_jobs()

    assert calls == []
    assert db.get_job(job_id)["status"] == JobStatus.COMPLETED


def test_exception_marks_job_failed(tmp_path, monkeypatch, notifications):
    def boom(*_):
        raise RuntimeError("kaput")

    monkeypatch.setattr(worker, "classify", boom)
    job_id = queue(tmp_path, "Movie.en.srt", 300)

    worker.process_pending_jobs()

    job = db.get_job(job_id)
    assert job["status"] == JobStatus.FAILED
    assert job["error"] == "kaput"


def test_should_blacklist():
    def result(score, issues=()):
        return SimpleNamespace(score=score, issues=list(issues))

    assert worker._should_blacklist(result(0.9, ["Likely mislabeled: 300 cues"]))
    assert worker._should_blacklist(result(0.9, ["Subtitle file is empty (0 cues)"]))
    assert worker._should_blacklist(result(0.1))
    assert not worker._should_blacklist(result(0.5, ["something else"]))
