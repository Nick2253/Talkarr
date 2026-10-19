import json
import os
from types import SimpleNamespace

import pytest

from talkarr import db, settings, worker
from talkarr.config import Config
from talkarr.db import JobStatus
from talkarr.forced import sources
from talkarr.forced import subtitles as S
from talkarr.forced.audio import Region
from tests.conftest import make_srt
from tests.test_eval import make_film


def sha(path):
    import hashlib

    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def trial_sync(monkeypatch):
    """Fake ffsubsync trial run (worker) and in-place sync (applying a sync fix)."""
    calls = {"trial": 0, "inplace": 0}

    def install(offset=1.25, confidence=0.9, success=True):
        def run(sub, media, out, **_):
            calls["trial"] += 1
            return SimpleNamespace(
                success=success,
                offset=offset,
                confidence=confidence,
                framerate=1.0,
                error=None if success else "no audio",
            )

        def inplace(path, media, sub_type):
            calls["inplace"] += 1
            S_cues = S.load(path)
            with open(path, "w") as f:
                f.write(S.compose(S.transform(S_cues, offset=offset)))
            return SimpleNamespace(success=True, offset=offset, confidence=confidence)

        monkeypatch.setattr(worker, "run_ffsubsync", run)
        monkeypatch.setattr("talkarr.sync.sync_subtitle", inplace)
        return calls

    return install


def regular(tmp_path, lines=40, encoding="utf-8", ad=False):
    texts = [f"Line number {i} of the dialogue." for i in range(lines)]
    if ad:
        texts[0] = "Subtitles by SomeGroup"
    texts[-1] = "Café crème"
    sub = tmp_path / "Movie.en.srt"
    sub.write_bytes(make_srt(texts).encode(encoding))
    (tmp_path / "Movie.mkv").write_bytes(b"\0")
    db.create_job("j", str(sub), str(tmp_path / "Movie.mkv"), "en")
    return sub


def job():
    j = db.get_job("j")
    return j, json.loads(j["proposal"])["fixes"] if j["proposal"] else []


def test_manual_mode_proposes_and_writes_nothing(tmp_path, trial_sync):
    calls = trial_sync()
    sub = regular(tmp_path, encoding="cp1252", ad=True)
    before = sha(sub)

    worker.process_pending_jobs()

    j, fixes = job()
    assert sha(sub) == before
    assert j["status"] == JobStatus.COMPLETED and j["outcome"] == "pending"
    assert [(f["kind"], f["tier"]) for f in fixes] == [("clean", "safe"), ("sync", "confident")]
    assert fixes[0]["summary"] == "Clean: strip 1 ad, cp1252 → UTF-8"
    assert fixes[1]["summary"] == "Sync +1.25 s"
    assert calls == {"trial": 1, "inplace": 0}
    assert not list((tmp_path / "cache" / "tmp").iterdir())


def test_safe_auto_applies_only_safe_fixes(tmp_path, trial_sync):
    calls = trial_sync()
    settings.set_mode("safe_auto")
    sub = regular(tmp_path, encoding="cp1252", ad=True)

    worker.process_pending_jobs()

    j, fixes = job()
    assert [f["state"] for f in fixes] == ["applied", "proposed"]
    assert j["outcome"] == "partial"
    assert "Subtitles by SomeGroup" not in sub.read_text(encoding="utf-8")
    assert calls["inplace"] == 0
    backups = json.loads(j["backup"])
    assert os.path.isfile(backups[0]["copy"]) and backups[0]["by"] == "safe_auto"


def test_full_auto_applies_confident_fixes(tmp_path, trial_sync):
    calls = trial_sync()
    settings.set_mode("full_auto")
    sub = regular(tmp_path, encoding="cp1252", ad=True)
    before = S.load(sub)

    worker.process_pending_jobs()

    j, fixes = job()
    assert [f["state"] for f in fixes] == ["applied", "applied"]
    # The sync applied is the trial's correction, not a second ffsubsync run.
    assert j["outcome"] == "applied" and calls == {"trial": 1, "inplace": 0}
    assert S.load(sub)[0].start == pytest.approx(before[1].start + 1.25)  # ad cue gone


def test_low_confidence_sync_is_uncertain_and_waits(tmp_path, trial_sync):
    trial_sync(confidence=0.5)
    settings.set_mode("full_auto")
    regular(tmp_path)
    worker.process_pending_jobs()
    j, fixes = job()
    assert [(f["kind"], f["tier"], f["state"]) for f in fixes] == [
        ("sync", "uncertain", "proposed")
    ]
    assert j["outcome"] == "pending"


def test_tiny_sync_offset_is_not_a_fix(tmp_path, trial_sync):
    trial_sync(offset=0.05)
    regular(tmp_path)
    worker.process_pending_jobs()
    j, fixes = job()
    assert fixes == [] and j["outcome"] == "nothing"


def test_sync_check_failure_is_noted(tmp_path, trial_sync):
    trial_sync(success=False)
    regular(tmp_path)
    worker.process_pending_jobs()
    j, _ = job()
    assert "sync check failed: no audio" in json.loads(j["actions_taken"])


def test_missing_video_is_noted(tmp_path, trial_sync):
    trial_sync()
    sub = tmp_path / "Alone.en.srt"
    sub.write_text(make_srt(["a"] * 30))
    db.create_job("j", str(sub), "", "en")
    worker.process_pending_jobs()
    j, _ = job()
    assert "sync check failed: no video found next to the subtitle" in json.loads(
        j["actions_taken"]
    )


def test_exception_marks_job_failed(tmp_path, monkeypatch):
    def boom(*_):
        raise RuntimeError("kaput")

    monkeypatch.setattr(worker, "classify", boom)
    regular(tmp_path)
    worker.process_pending_jobs()
    j = db.get_job("j")
    assert j["status"] == JobStatus.FAILED and j["error"] == "kaput"


def test_subtitle_gone_before_its_turn_is_skipped(tmp_path):
    os.unlink(regular(tmp_path))
    worker.process_pending_jobs()
    j = db.get_job("j")
    assert j["status"] == JobStatus.SKIPPED and j["error"] == worker.GONE


# --- forced subtitles ------------------------------------------------------------------


@pytest.fixture
def forced_film(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "FORCED_VERIFY_ENABLED", True)
    monkeypatch.setattr(Config, "SYNC_ENABLED", False)
    monkeypatch.setattr(Config, "ML_URL", "http://ml")
    d = make_film(tmp_path, "Alpha (2001)", 1)
    forced = next(d.glob("*.en.forced.srt"))
    truth = S.load(forced)

    def synced(video, reference, cache_dir):
        return S.load(reference), {"ok": True, "offset": 0.0}

    def regions(video, cache_dir, url):
        return [
            Region(c.start - 0.1, c.end + 0.1, 0.01, (("ru", 0.9), ("uk", 0.05))) for c in truth
        ], {"ok": True, "duration": 7200.0}

    monkeypatch.setattr(sources, "synced_reference", synced)
    monkeypatch.setattr(sources, "film_regions", regions)
    return forced, truth


def queue(path):
    db.create_job("j", str(path), "", "en")
    worker.process_pending_jobs()


def test_in_sync_forced_sub_needs_nothing(forced_film):
    forced, _ = forced_film
    queue(forced)
    j, fixes = job()
    assert j["verdict"] == "in_sync" and fixes == [] and j["outcome"] == "nothing"
    ribbon = json.loads(j["report"])["ribbon"]
    assert ribbon["runtime"] == 7200.0 and len(ribbon["cues"]) == 20 and ribbon["speech"]


def test_corroborated_retime_is_safe_and_applied_in_safe_auto(forced_film):
    forced, truth = forced_film
    forced.write_text(S.compose(S.transform(truth, offset=6.0)))
    settings.set_mode("safe_auto")

    queue(forced)

    j, fixes = job()
    assert [(f["kind"], f["tier"], f["state"]) for f in fixes] == [("retime", "safe", "applied")]
    assert fixes[0]["summary"] == "Retime −6.00 s"
    restored = S.load(forced)
    assert abs(restored[0].start - truth[0].start) < 0.01


@pytest.mark.parametrize(
    ("action", "full_sub", "expected"),
    [
        ("rename_or_remove_and_backup", False, "rename"),
        ("rename_or_remove_and_backup", True, "remove"),
        ("remove_and_backup", False, "remove"),
        ("flag", False, None),
        ("remove", False, "remove"),  # older names still work
        ("rename_or_remove", False, "rename"),
    ],
)
def test_mislabelled_forced_sub(tmp_path, forced_film, monkeypatch, action, full_sub, expected):
    forced, _ = forced_film
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", action)
    if not full_sub:
        for p in forced.parent.glob("*.en.srt"):
            p.unlink()
    forced.write_text(make_srt([f"dialogue line {i}" for i in range(300)]))
    settings.set_mode("full_auto")

    queue(forced)

    j, fixes = job()
    assert j["verdict"] == "mislabelled"
    if expected is None:
        assert fixes == [] and forced.exists()
        return
    assert [(f["kind"], f["state"]) for f in fixes] == [(expected, "applied")]
    assert not forced.exists()
    if expected == "rename":
        assert (forced.parent / forced.name.replace(".en.forced.srt", ".en.srt")).exists()


def test_queue_order_puts_rechecks_and_bazarr_before_a_scan_backlog(tmp_path, monkeypatch):
    from talkarr import review

    order = []
    monkeypatch.setattr(
        worker,
        "_process_job",
        lambda job: (order.append(job["id"]), db.update_job_status(job["id"], JobStatus.COMPLETED)),
    )
    db.create_job("scan1", "/m/1.srt", "", "en", source="bulk")
    db.create_job("scan2", "/m/2.srt", "", "en", source="bulk")
    db.create_job("old", "/m/0.srt", "", "en", source="bulk")
    db.update_job_status("old", JobStatus.COMPLETED)
    review.recheck_job("old")
    db.create_job("hook", "/m/3.srt", "", "en", source="bazarr")

    worker.process_pending_jobs()

    assert order == ["old", "hook", "scan1", "scan2"]


def test_jobs_queued_while_working_are_picked_up(tmp_path, monkeypatch):
    order = []

    def process(job):
        order.append(job["id"])
        db.update_job_status(job["id"], JobStatus.COMPLETED)
        if job["id"] == "a":
            db.create_job("late", "/m/late.srt", "", "en", source="bazarr")

    monkeypatch.setattr(worker, "_process_job", process)
    db.create_job("a", "/m/a.srt", "", "en", source="bulk")
    db.create_job("b", "/m/b.srt", "", "en", source="bulk")
    worker.process_pending_jobs()
    assert order == ["a", "late", "b"]
