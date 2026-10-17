import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from talkarr import db, worker
from talkarr.config import Config
from talkarr.db import JobStatus
from talkarr.forced import service, sources
from talkarr.forced import subtitles as S
from talkarr.forced.audio import Region
from talkarr.forced.decide import combine
from talkarr.forced.verify import Report, Verdict
from tests.conftest import make_srt
from tests.test_eval import make_film


def report(verdict, scale=1.0, offset=0.0, review=False, reason="r"):
    return Report(Verdict(verdict), reason, 10, scale=scale, offset=offset, review=review)


# --- combine -------------------------------------------------------------------------


def test_mislabelled_wins():
    d = combine(report("in_sync"), report("mislabelled"))
    assert d.verdict == Verdict.MISLABELLED and d.source == "audio"


def test_text_and_audio_agree():
    d = combine(report("retimed", offset=2.0), report("retimed", offset=2.4))
    assert d.verdict == Verdict.RETIMED and not d.review
    assert d.source == "text+audio" and d.offset == 2.0
    assert "audio agrees" in d.reason


def test_agreeing_framerate_fix_needs_no_review():
    text = report(
        "retimed",
        scale=23.976 / 25,
        offset=-0.9,
        review=True,
        reason="x (large offset or framerate change: review)",
    )
    d = combine(text, report("retimed", scale=23.976 / 25, offset=-1.2))
    assert d.verdict == Verdict.RETIMED and not d.review
    assert "audio agrees" in d.reason and "review" not in d.reason.split(";")[0]


def test_broad_agreement_star_wars():
    """Star Wars: text 25->23.976 -0.90 s (residual 0.17 s); audio 25->24 -4.31 s.
    Same PAL correction, cues within ~3 s: the precise text fit stands."""
    text = report(
        "retimed",
        scale=0.95904,
        offset=-0.9,
        review=True,
        reason="29 lines (large offset or framerate change: review)",
    )
    text.residual_p90 = 0.173
    audio = report(
        "retimed",
        scale=0.96,
        offset=-4.31,
        reason="26/30 cues (large offset or framerate change: review)",
    )
    d = combine(text, audio)
    assert d.verdict == Verdict.RETIMED and not d.review
    assert d.scale == 0.95904 and "broadly agrees" in d.reason
    assert "review" not in d.reason


def test_broad_agreement_requires_a_precise_text_fit():
    text = report("retimed", scale=0.95904, offset=-0.9, review=True)
    text.residual_p90 = 0.5
    d = combine(text, report("retimed", scale=0.96, offset=-4.31))
    assert d.review and "disagrees" in d.reason


def test_text_and_audio_disagree_needs_review():
    d = combine(report("in_sync"), report("retimed", scale=23.976 / 25))
    assert d.verdict == Verdict.IN_SYNC and d.review
    assert "audio disagrees" in d.reason


def test_text_alone_and_audio_alone():
    d = combine(report("retimed", offset=5), report("unverifiable", reason="no speech"))
    assert d.verdict == Verdict.RETIMED and d.source == "text"
    assert "audio couldn't confirm (no speech)" in d.reason
    d = combine(report("unverifiable", reason="lines missing"), report("in_sync"))
    assert d.verdict == Verdict.IN_SYNC and d.source == "audio"
    assert "text couldn't tell (lines missing)" in d.reason
    assert combine(None, report("retimed", offset=3)).verdict == Verdict.RETIMED


def test_inconclusive_audio_fit_corroborates_same_correction():
    """Spy Game: text wants 24->23.976 and -1.12 s (review); audio alone can't beat
    chance but its best fit is the same correction with +84% cue time on speech."""
    text = report(
        "retimed",
        scale=0.999,
        offset=-1.125,
        review=True,
        reason="26 lines line up (large offset or framerate change: review)",
    )
    audio = report("unverifiable", scale=0.999, offset=-1.583, reason="no alignment beats chance")
    audio.details = {"overlap_gain": 0.845}
    d = combine(text, audio)
    assert d.verdict == Verdict.RETIMED and not d.review
    assert "audio independently finds the same correction" in d.reason
    assert "review" not in d.reason


@pytest.mark.parametrize(
    ("scale", "offset", "gain"),
    [(1.0, -1.583, 0.845), (0.999, 40.0, 0.845), (0.999, -1.583, 0.05)],
)
def test_no_corroboration_from_different_or_weak_audio_fits(scale, offset, gain):
    text = report("retimed", scale=0.999, offset=-1.125, review=True)
    audio = report("unverifiable", scale=scale, offset=offset)
    audio.details = {"overlap_gain": gain}
    d = combine(text, audio)
    assert d.review and "audio couldn't confirm" in d.reason


def test_text_mismatch_is_flagged_for_review():
    d = combine(report("mismatch"), report("unverifiable"))
    assert d.verdict == Verdict.MISMATCH and d.review


def test_nothing_conclusive():
    d = combine(report("unverifiable", reason="a"), report("unverifiable", reason="b"))
    assert d.verdict == Verdict.UNVERIFIABLE and d.reason == "text: a; audio: b"
    assert combine().reason == "no evidence available"
    assert combine().as_dict()["verdict"] == "unverifiable"


# --- verify_forced -------------------------------------------------------------------


@pytest.fixture
def film(tmp_path):
    d = make_film(tmp_path, "Alpha (2001)", 1)
    forced = next(d.glob("*.en.forced.srt"))
    return str(forced), str(next(d.glob("*.mkv")))


def fake_sources(monkeypatch, sync_ok=True, regions=True):
    def synced(video, reference, cache_dir):
        if not sync_ok:
            return None, {"ok": False, "error": "ffsubsync exit 1"}
        return S.load(reference), {"ok": True, "offset": 0.0}

    def film_regions(video, cache_dir, url):
        if not regions:
            raise ConnectionError("talkarr-ml down")
        forced = S.load(film_regions.forced)
        return [
            Region(c.start - 0.1, c.end + 0.1, 0.01, (("ru", 0.9), ("uk", 0.05))) for c in forced
        ], {"ok": True}

    monkeypatch.setattr(sources, "synced_reference", synced)
    monkeypatch.setattr(sources, "film_regions", film_regions)
    return film_regions


def test_verify_forced_with_both_paths(film, tmp_path, monkeypatch):
    forced, video = film
    fake_sources(monkeypatch).forced = forced
    decision, details = service.verify_forced(forced, video, str(tmp_path / "c"), ml_url="x")
    assert decision.verdict == Verdict.IN_SYNC and decision.source == "text+audio"
    assert details["text"]["verdict"] == "in_sync"
    assert details["audio"]["verdict"] == "in_sync"


def test_verify_forced_unsynced_reference_flags_retimes_for_review(film, tmp_path, monkeypatch):
    forced, video = film
    shifted = S.transform(S.load(forced), offset=6.0)
    with open(forced, "w") as f:
        f.write(S.compose(shifted))
    fake_sources(monkeypatch, sync_ok=False)
    decision, details = service.verify_forced(forced, video, str(tmp_path / "c"))
    assert decision.verdict == Verdict.RETIMED and decision.review
    assert "could not be synced" in decision.reason
    assert "audio" not in details  # no ml_url


def test_verify_forced_survives_ml_outage(film, tmp_path, monkeypatch):
    forced, video = film
    fake_sources(monkeypatch, regions=False)
    decision, details = service.verify_forced(forced, video, str(tmp_path / "c"), ml_url="x")
    assert decision.verdict == Verdict.IN_SYNC
    assert "talkarr-ml down" in details["audio_analysis"]["error"]


# --- db migration ----------------------------------------------------------------------


def test_migration_adds_columns_to_old_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, subtitle_path TEXT NOT NULL, "
        "media_path TEXT, language TEXT, sub_type TEXT, source TEXT, status TEXT, "
        "sync_offset REAL, sync_confidence REAL, validation_score REAL, "
        "validation_notes TEXT, actions_taken TEXT, error TEXT, created_at REAL, "
        "updated_at REAL)"
    )
    con.execute(
        "INSERT INTO jobs (id, subtitle_path, status) VALUES ('old', '/m/x.srt', 'completed')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(Config, "DATABASE_PATH", str(path))
    db._local.db = None

    db.init_db()
    db.update_job_report("old", "in_sync", False, {"reason": "ok"})

    job = db.get_job("old")
    assert job["verdict"] == "in_sync" and json.loads(job["report"]) == {"reason": "ok"}


# --- worker in dry-run -----------------------------------------------------------------


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_dry_run_never_writes_the_library(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DRY_RUN", True)
    sub = tmp_path / "Movie.en.srt"
    sub.write_bytes(
        b"\xef\xbb\xbf"
        + make_srt(["Subtitles by Someone"] + [f"line {i}" for i in range(40)]).encode()
    )
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"\0")
    before = digest(sub)

    def fake_run(subtitle, media, out, **_):
        with open(out, "w") as f:
            f.write("synced")
        return SimpleNamespace(success=True, skipped=False, offset=1.5, confidence=0.9, error=None)

    monkeypatch.setattr(worker, "run_ffsubsync", fake_run)
    monkeypatch.setattr(worker, "sync_subtitle", lambda *a: pytest.fail("must not sync in place"))
    db.create_job("j", str(sub), str(video), "en")

    worker.process_pending_jobs()

    job = db.get_job("j")
    actions = json.loads(job["actions_taken"])
    assert digest(sub) == before
    assert any(a.startswith("would clean: 1 ads removed") for a in actions)
    assert "would sync: offset=1.50s" in actions
    assert job["sync_offset"] == 1.5
    assert not list((tmp_path / "cache" / "tmp").iterdir())  # trial output removed


def test_forced_job_records_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DRY_RUN", True)
    monkeypatch.setattr(Config, "FORCED_VERIFY_ENABLED", True)
    d = make_film(tmp_path, "Alpha (2001)", 1)
    forced = next(d.glob("*.en.forced.srt"))
    before = digest(forced)
    fake_sources(monkeypatch)
    db.create_job("f", str(forced), "", "en")

    worker.process_pending_jobs()

    job = db.get_job("f")
    assert job["status"] == JobStatus.COMPLETED
    assert job["verdict"] == "in_sync" and job["review"] == 0
    assert "forced: in_sync" in json.loads(job["actions_taken"])
    assert json.loads(job["report"])["text"]["verdict"] == "in_sync"
    assert digest(forced) == before


def test_forced_job_without_video(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "FORCED_VERIFY_ENABLED", True)
    sub = tmp_path / "Lonely.en.forced.srt"
    sub.write_text(make_srt(["a b c d e"] * 5))
    db.create_job("f", str(sub), "", "en")
    worker.process_pending_jobs()
    assert db.get_job("f")["verdict"] == "unverifiable"


def test_dry_run_flags_instead_of_blacklisting(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DRY_RUN", True)
    monkeypatch.setattr(Config, "SYNC_ENABLED", False)
    sub = tmp_path / "Movie.en.forced.srt"
    sub.write_text(make_srt([f"line {i}" for i in range(300)]))
    db.create_job("f", str(sub), "", "en")
    worker.process_pending_jobs()
    assert db.get_job("f")["status"] == JobStatus.FLAGGED
