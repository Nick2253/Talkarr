import json
import os
from types import SimpleNamespace

import pytest

from talkarr import db, fixes, review, settings
from talkarr.config import Config
from talkarr.forced import subtitles as S
from tests.conftest import make_srt


@pytest.mark.parametrize(
    ("scale", "label"),
    [
        (1.0, None),
        (23.976 / 25, "25 → 23.976"),
        (25 / 23.976, "23.976 → 25"),
        (0.999, "24 → 23.976"),
        (24 / 25, "25 → 24"),
        (1.3, "×1.30000"),
    ],
)
def test_framerate_label(scale, label):
    assert fixes.framerate_label(scale) == label


def test_retime_summary():
    assert fixes.retime_summary(23.976 / 25, -0.9) == "Retime 25 → 23.976 fps, −0.90 s"
    assert fixes.retime_summary(1.0, 2.5) == "Retime +2.50 s"
    assert fixes.retime_summary(0.999, 0.0) == "Retime 24 → 23.976 fps"


def clean_result(**kw):
    base = dict(
        success=True,
        changed=True,
        ads_removed=0,
        encoding_fixed=False,
        reencoded=False,
        source_encoding="utf-8",
        bom_stripped=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def decision(verdict, review=False, source="text", scale=1.0, offset=0.0):
    return SimpleNamespace(
        verdict=verdict, review=review, source=source, scale=scale, offset=offset, reason="r"
    )


def test_build_clean_variants():
    f = fixes.build(
        "/m/a.en.srt", "regular", clean=clean_result(ads_removed=2, encoding_fixed=True)
    )
    assert f[0]["summary"] == "Clean: strip 2 ads, repair garbled characters"
    f = fixes.build("/m/a.en.srt", "regular", clean=clean_result(bom_stripped=True))
    assert f[0]["summary"] == "Clean: remove byte-order mark"
    assert fixes.build("/m/a.en.srt", "regular", clean=clean_result(changed=False)) == []


@pytest.mark.parametrize(
    ("dec", "tier"),
    [
        (decision("retimed", source="text+audio"), "safe"),
        (decision("retimed"), "confident"),
        (decision("retimed", review=True, source="text+audio"), "uncertain"),
    ],
)
def test_build_retime_tiers(dec, tier):
    f = fixes.build("/m/a.en.forced.srt", "forced", decision=dec)
    assert [(x["kind"], x["tier"]) for x in f] == [("retime", tier)]


def test_build_ignores_sync_for_forced_and_other_verdicts():
    sync = SimpleNamespace(success=True, offset=3.0, confidence=1.0, framerate=1.0)
    assert (
        fixes.build("/m/a.en.forced.srt", "forced", sync=sync, decision=decision("in_sync")) == []
    )


def test_build_framerate_only_sync():
    sync = SimpleNamespace(success=True, offset=0.0, confidence=0.9, framerate=1.042)
    assert [x["kind"] for x in fixes.build("/m/a.en.srt", "regular", sync=sync)] == ["sync"]


def test_auto_indices_and_outcome():
    f = [
        fixes._fix("clean", "c", "safe", {}),
        fixes._fix("sync", "s", "confident", {}),
        fixes._fix("retime", "r", "uncertain", {}),
    ]
    assert fixes.auto_indices("manual", f) == []
    assert fixes.auto_indices("safe_auto", f) == [0]
    assert fixes.auto_indices("full_auto", f) == [0, 1]
    assert fixes.outcome([]) == "nothing"
    assert fixes.outcome(f) == "pending"
    f[0]["state"] = "applied"
    assert fixes.outcome(f) == "partial"
    f[1]["state"] = f[2]["state"] = "dismissed"
    assert fixes.outcome(f) == "applied"
    assert fixes.outcome([{**f[1]}]) == "dismissed"
    assert fixes.outcome([{**f[0], "state": "failed"}]) == "failed"


# --- apply / revert through review -------------------------------------------------------


def make_job(tmp_path, fix_list, name="Film.en.forced.srt", texts=None):
    sub = tmp_path / name
    sub.write_text(make_srt(texts or [f"line {i} here" for i in range(10)]))
    os.chmod(sub, 0o640)
    db.create_job("j", str(sub), "", "en")
    db.update_job_proposal(
        "j",
        {"fingerprint": fixes.fingerprint(str(sub)), "fixes": fix_list},
        fixes.outcome(fix_list),
    )
    return sub


def state():
    j = db.get_job("j")
    return j, json.loads(j["proposal"])["fixes"]


def test_apply_retime_then_revert(tmp_path):
    sub = make_job(tmp_path, [fixes._fix("retime", "r", "safe", {"scale": 1.0, "offset": 5.0})])
    original = sub.read_bytes()

    review.apply_job("j")
    j, f = state()
    assert f[0]["state"] == "applied" and j["outcome"] == "applied"
    assert S.load(str(sub))[0].start == pytest.approx(15.0)
    assert oct(os.stat(sub).st_mode & 0o777) == "0o640"

    review.revert_job("j")
    j, f = state()
    assert sub.read_bytes() == original
    assert f[0]["state"] == "proposed" and j["outcome"] == "pending"
    review.apply_job("j")  # fingerprint refreshed by the revert: applying again works


def test_adjusted_retime(tmp_path):
    sub = make_job(
        tmp_path, [fixes._fix("retime", "r", "uncertain", {"scale": 1.0, "offset": 5.0})]
    )
    review.apply_job("j", [0], overrides={"scale": 23.976 / 25, "offset": -1.0})
    _, f = state()
    assert f[0]["params"]["adjusted"] and f[0]["summary"] == "Retime 25 → 23.976 fps, −1.00 s"
    assert S.load(str(sub))[0].start == pytest.approx(10 * 23.976 / 25 - 1.0, abs=0.002)


def test_sync_adjusted_becomes_retime(tmp_path):
    sub = make_job(tmp_path, [fixes._fix("sync", "s", "confident", {"offset": 3.0})], "F.en.srt")
    review.apply_job("j", [0], overrides={"offset": 1.5})
    assert S.load(str(sub))[0].start == pytest.approx(11.5)


def test_apply_clean(tmp_path):
    sub = make_job(
        tmp_path,
        [fixes._fix("clean", "c", "safe", {})],
        "F.en.srt",
        texts=["Downloaded from OpenSubtitles"] + [f"line {i}" for i in range(9)],
    )
    review.apply_job("j")
    assert "OpenSubtitles" not in sub.read_text()


def test_rename_and_revert(tmp_path):
    target = tmp_path / "Film.en.srt"
    sub = make_job(tmp_path, [fixes._fix("rename", "n", "confident", {"to": str(target)})])
    review.apply_job("j")
    assert not sub.exists() and target.exists()
    review.revert_job("j")
    assert sub.exists() and not target.exists()


def test_rename_refuses_to_overwrite(tmp_path):
    target = tmp_path / "Film.en.srt"
    target.write_text("existing")
    make_job(tmp_path, [fixes._fix("rename", "n", "confident", {"to": str(target)})])
    with pytest.raises(review.ReviewError, match="already exists"):
        review.apply_job("j")
    j, f = state()
    assert f[0]["state"] == "failed" and j["outcome"] == "failed"
    assert target.read_text() == "existing"


def test_remove_and_revert(tmp_path):
    sub = make_job(tmp_path, [fixes._fix("remove", "x", "confident", {})])
    review.apply_job("j")
    assert not sub.exists()
    review.revert_job("j")
    assert sub.exists()


def test_refuses_when_subtitle_changed(tmp_path):
    sub = make_job(tmp_path, [fixes._fix("retime", "r", "safe", {"scale": 1.0, "offset": 5.0})])
    sub.write_text(make_srt(["something else entirely"] * 3))
    with pytest.raises(review.ReviewError, match="changed since it was checked"):
        review.apply_job("j")
    sub.unlink()
    with pytest.raises(review.ReviewError, match="no longer exists"):
        review.apply_job("j")


def test_dismiss_recheck_and_errors(tmp_path):
    make_job(
        tmp_path, [fixes._fix("clean", "c", "safe", {}), fixes._fix("sync", "s", "uncertain", {})]
    )
    review.dismiss_job("j", [1])
    j, f = state()
    assert [x["state"] for x in f] == ["proposed", "dismissed"] and j["outcome"] == "pending"
    review.dismiss_job("j")
    assert state()[0]["outcome"] == "dismissed"
    with pytest.raises(review.ReviewError, match="nothing left"):
        review.apply_job("j")
    with pytest.raises(review.ReviewError, match="nothing has been applied"):
        review.revert_job("j")
    review.recheck_job("j")
    assert db.get_job("j")["status"] == "pending"
    with pytest.raises(review.ReviewError, match="no such job"):
        review.apply_job("nope")
    with pytest.raises(review.ReviewError, match="no such job"):
        review.recheck_job("nope")
    db.create_job("k", "/m/x.srt", "", "en")
    with pytest.raises(review.ReviewError, match="no proposed fixes"):
        review.apply_job("k")


def test_unknown_fix_kind_fails_cleanly(tmp_path):
    make_job(tmp_path, [fixes._fix("teleport", "t", "safe", {})])
    with pytest.raises(review.ReviewError, match="unknown fix"):
        review.apply_job("j")


def test_revert_without_backup_copy():
    with pytest.raises(fixes.ApplyError):
        fixes.revert({}, {"copy": "/nonexistent"})


# --- settings ----------------------------------------------------------------------------


def test_mode_defaults_and_switching(monkeypatch):
    assert settings.get_mode() == "manual"
    monkeypatch.setattr(Config, "MODE", "full_auto")
    assert settings.get_mode() == "full_auto"
    monkeypatch.setattr(Config, "MODE", "bogus")
    assert settings.get_mode() == "manual"
    settings.set_mode("safe_auto")
    assert settings.get_mode() == "safe_auto"
    with pytest.raises(ValueError):
        settings.set_mode("yolo")


def test_mislabelled_action_names(monkeypatch):
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", "explode")
    assert settings.mislabelled_action() == "flag"
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", "remove")
    assert settings.mislabelled_action() == "remove"
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", "rename_or_remove")
    assert settings.mislabelled_action() == "rename_or_remove_and_backup"


def test_configuration_masks_secrets_and_tracks_source(monkeypatch):
    monkeypatch.setenv("MEDIA_PATH", "/media")
    monkeypatch.setattr(Config, "BAZARR_API_KEY", "supersecret")
    rows = {r.label: r for r in settings.configuration()}
    assert rows["Media library"].from_env
    assert rows["Bazarr API key"].value == "set"
    assert "supersecret" not in json.dumps([r.__dict__ for r in rows.values()])
    assert rows["Webhook secret"].value == "not set"
    assert rows["Clean subtitles"].value == "on"


def test_configuration_shows_the_exact_compose_value(monkeypatch):
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", "rename_or_remove")
    rows = {r.label: r for r in settings.configuration()}
    assert rows["Mislabelled forced subtitles"].value == "rename_or_remove_and_backup"
