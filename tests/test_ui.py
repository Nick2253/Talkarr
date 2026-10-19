import html
import json
import time

import pytest

from talkarr import db, fixes, settings
from talkarr.forced import subtitles as S
from talkarr.ui import _ago, _tc, _tc_short, plain_reason, title_of
from tests.conftest import make_srt

HOST = "http://localhost"


@pytest.fixture
def library(media_dir):
    """A film with a retime proposal, a regular sub with two fixes, a flagged forced sub."""
    film = media_dir / "movies" / "Star Wars (1977)"
    film.mkdir(parents=True)
    forced = film / "Star Wars (1977).en.forced.srt"
    forced.write_text(make_srt([f"line {i}" for i in range(6)]))
    db.create_job("sw", str(forced), str(film / "Star Wars (1977).mkv"), "en")
    db.update_job_classification("sw", "forced")
    db.update_job_status("sw", "completed")
    db.update_job_report(
        "sw",
        "retimed",
        False,
        {
            "reason": "29 lines line up after scale 0.95904, offset -0.90s; audio agrees (x)",
            "text": {"verdict": "retimed", "inliers": 29, "forced_cues": 30, "reason": "t"},
            "audio": {"verdict": "retimed", "reason": "a"},
            "reference": "x",
            "reference_sync": {"ok": True, "offset": 0.09, "framerate": 0.959},
            "ribbon": {"runtime": 7500.0, "cues": [[10.0, 12.0]], "speech": [[9.0, 11.0]]},
        },
    )
    retime = fixes._fix(
        "retime",
        fixes.retime_summary(23.976 / 25, -0.9),
        "safe",
        {"scale": 23.976 / 25, "offset": -0.9},
    )
    db.update_job_proposal(
        "sw", {"fingerprint": fixes.fingerprint(str(forced)), "fixes": [retime]}, "pending"
    )

    regular = film / "Star Wars (1977).en.srt"
    regular.write_text(make_srt([f"line {i}" for i in range(30)]))
    db.create_job("rg", str(regular), "", "en")
    db.update_job_classification("rg", "regular")
    db.update_job_status("rg", "completed")
    db.update_job_sync("rg", 1.5, 0.9)
    two = [
        fixes._fix("clean", "Clean: strip 1 ad", "safe", {}),
        fixes._fix("sync", "Sync +1.50 s", "confident", {"offset": 1.5}),
    ]
    db.update_job_proposal(
        "rg", {"fingerprint": fixes.fingerprint(str(regular)), "fixes": two}, "pending"
    )

    db.create_job("mf", str(film / "Other.en.forced.srt"), "", "en")
    db.update_job_classification("mf", "forced")
    db.update_job_status("mf", "completed")
    db.update_job_report("mf", "mismatch", True, {"reason": "timings disagree"})
    db.update_job_proposal("mf", {"fixes": []}, "nothing")
    return forced, regular


class Page:
    def __init__(self, response):
        self.status_code = response.status_code
        self.text = html.unescape(response.data.decode())


def post(client, path, data=None, origin=HOST):
    headers = {"Origin": origin} if origin else {}
    return Page(client.post(path, data=data or {}, headers=headers, follow_redirects=True))


def get(client, path):
    return html.unescape(client.get(path).data.decode())


# --- pages ----------------------------------------------------------------------------------


def test_review_page(client, library):
    page = get(client, "/")
    assert "3 fixes" in page and "2 subtitles" in page
    assert "Retime 25 → 23.976 fps, −0.90 s" in page
    assert "29 of 30 lines match the full subtitle; the audio agrees." in page
    assert "Flagged, no automatic fix" in page and "Wrong film or cut" in page
    assert 'data-mode="manual"' in page and ">4<" not in page


def test_review_page_empty(client):
    page = get(client, "/")
    assert "Nothing is waiting for you." in page and "All caught up." in page


def test_job_page(client, library):
    page = get(client, "/job/sw")
    assert "Star Wars (1977)" in page and "Cue ribbon" in page
    assert "movies/Star Wars (1977)/Star Wars (1977).en.forced.srt" in page
    assert "Adjust fix" in page and 'value="-0.90"' in page
    assert "rescaled ×0.959" in page
    data = json.loads(page.split('id="cue-data">')[1].split("</script>")[0])
    assert data["runtime"] == 7500.0 and data["scale"] == pytest.approx(23.976 / 25)
    assert "00:00:10,000" in page  # first cue, timecode filter
    assert client.get("/job/nope").status_code == 404


def test_job_page_regular_and_flagged(client, library):
    page = get(client, "/job/rg")
    assert "Trial sync: ffsubsync would move it +1.50 s" in page and "Adjust fix" in page
    page = get(client, "/job/mf")
    assert "may belong to another cut" in page and "Needs a look" in page


def test_library_page_filters_and_scan_dirs(client, library):
    page = get(client, "/library")
    assert '<option value="' in page and ">movies</option>" in page and "Whole library" in page
    assert "Idle" in page
    page = get(client, "/library?verdict=mismatch")
    assert "Other.en.forced.srt" in page and "Star Wars (1977).en.srt" not in page
    page = get(client, "/library?q=nothing-matches")
    assert "No subtitles match." in page


def test_settings_page(client, monkeypatch):
    monkeypatch.setenv("MEDIA_PATH", "/media")
    page = get(client, "/settings")
    assert "Safe Auto" in page and "Full Auto" in page
    assert "MEDIA_PATH" in page and "compose" in page and "MISLABELLED_ACTION" in page


# --- actions --------------------------------------------------------------------------------


def test_apply_all_from_queue(client, library):
    _, regular = library
    before = S.load(str(regular))
    r = post(client, "/job/rg/apply", {"next": "/"})
    assert "Couldn't apply" not in r.text  # the proposed correction needs no video
    assert S.load(str(regular))[0].start == pytest.approx(before[0].start + 1.5)
    page = get(client, "/activity")
    assert "Star Wars (1977).en.srt" in page


def test_apply_adjusted_retime_then_restore(client, library):
    forced, _ = library
    original = forced.read_bytes()
    r = post(
        client, "/job/sw/apply", {"fix": "0", "scale": "1.0", "offset": "2.5", "next": "/job/sw"}
    )
    assert "Applied: Retime +2.50 s." in r.text
    assert S.load(str(forced))[0].start == pytest.approx(12.5)
    assert db.get_job("sw")["outcome"] == "applied"
    page = get(client, "/activity")
    assert "Restore original" in page and "by you" in page

    r = post(client, "/job/sw/revert", {"next": "/job/sw"})
    assert "Restored the original" in r.text
    assert forced.read_bytes() == original


def test_apply_rejects_bad_numbers(client, library):
    r = post(client, "/job/sw/apply", {"fix": "0", "offset": "soon"})
    assert "Offset must be a number" in r.text
    assert post(client, "/job/sw/apply", {"fix": "x"}).status_code == 400


def test_dismiss_recheck_and_errors(client, library):
    r = post(client, "/job/rg/dismiss", {"fix": "1"})
    assert "Dismissed" in r.text
    assert json.loads(db.get_job("rg")["proposal"])["fixes"][1]["state"] == "dismissed"
    assert "Queued to be checked again" in post(client, "/job/rg/recheck").text
    assert "Couldn't restore" in post(client, "/job/sw/revert").text
    assert post(client, "/job/nope/dismiss").status_code == 404
    assert post(client, "/job/nope/recheck").status_code == 404


def test_mode_switch(client):
    r = post(client, "/settings/mode", {"mode": "full_auto"})
    assert "Mode is now Full Auto" in r.text
    assert settings.get_mode() == "full_auto"
    assert 'data-mode="full_auto"' in get(client, "/")
    assert "Pick one of the three modes" in post(client, "/settings/mode", {"mode": "yolo"}).text


def test_scan_runs_in_the_background(client, library, media_dir):
    from talkarr import scanner

    (media_dir / "movies" / "Star Wars (1977)" / "Star Wars (1977).mkv").write_bytes(b"")
    r = post(client, "/scan", {"directory": str(media_dir), "filter": "all", "requeue": "1"})
    assert "Scanning the whole library." in r.text
    scanner.wait(10)
    state = scanner.state()
    assert not state["running"] and state["queued"] == 2 and state["seen"] >= 2
    page = get(client, "/library")
    assert "Last scan (the whole library, just now): queued 2 of" in page


def test_scan_refuses_bad_folders_and_a_second_scan(client, library, media_dir, monkeypatch):
    from talkarr import scanner

    r = post(client, "/scan", {"directory": "/etc", "filter": "all"})
    assert "outside the media library" in r.text
    monkeypatch.setattr(
        scanner,
        "_state",
        {
            "running": True,
            "label": "movies",
            "started": 0,
            "folders": 3,
            "seen": 7,
            "queued": 2,
            "filter": "forced",
        },
    )
    r = post(client, "/scan", {"directory": str(media_dir), "filter": "forced"})
    assert "A scan of movies is already running." in r.text
    page = get(client, "/library")
    assert "Scanning movies" in page and "2</span> queued so far" in page
    assert 'disabled title="A scan is already running"' in page


def test_scan_failure_is_reported(client, library, media_dir, monkeypatch):
    from talkarr import app as app_module
    from talkarr import scanner

    def boom(*a, **k):
        raise OSError("share went away")

    monkeypatch.setattr(app_module, "queue_scan", boom)
    post(client, "/scan", {"directory": str(media_dir), "filter": "all"})
    scanner.wait(10)
    assert "failed: share went away" in get(client, "/library")


def test_cross_site_posts_are_refused(client, library):
    assert (
        client.post("/job/sw/apply", headers={"Origin": "http://evil.example"}).status_code == 403
    )
    assert (
        client.post(
            "/settings/mode",
            data={"mode": "full_auto"},
            headers={"Referer": "http://evil.example/x"},
        ).status_code
        == 403
    )
    assert settings.get_mode() == "manual"


def test_redirect_target_must_be_local(client, library):
    r = client.post("/job/rg/recheck", data={"next": "//evil.example/"}, headers={"Origin": HOST})
    assert r.headers["Location"].endswith("/job/rg")


# --- helpers ------------------------------------------------------------------------------------


def test_filters():
    assert _tc(3725.5) == "01:02:05,500"
    assert _tc_short(7498) == "2:04:58"
    assert _ago(None) == "–" and _ago(time.time()) == "just now"
    assert _ago(time.time() - 120) == "2 min ago" and _ago(time.time() - 7200) == "2 h ago"
    assert _ago(time.time() - 86400 * 3) == "3 days ago"


def test_title_of():
    assert title_of("/media/movies/Heat (1995)/Heat.en.srt") == "Heat (1995)"
    assert title_of("/media/tv/Barry/Season 01/Barry.S01E08.en.forced.srt") == "Barry · Season 01"


@pytest.mark.parametrize(
    ("verdict", "report", "expected"),
    [
        (
            "retimed",
            {"audio": {"verdict": "retimed", "inliers": 26, "forced_cues": 30}, "reason": "x"},
            "26 of 30 lines sit on foreign speech in the audio.",
        ),
        ("retimed", {"reason": "only technical"}, "only technical"),
        (
            "mislabelled",
            {"forced_cues": 900, "reason": "900 cues"},
            "It is a full subtitle saved as forced (900 lines).",
        ),
        (
            "unverifiable",
            {"reason": "no reference subtitle"},
            "No full subtitle to compare with, and the audio check couldn't tell.",
        ),
        (
            "unverifiable",
            {"reference": "x", "text": {}, "reason": "r"},
            "Neither the full subtitle nor the audio could confirm it.",
        ),
    ],
)
def test_plain_reason(verdict, report, expected):
    assert plain_reason({"verdict": verdict}, report) == expected


def test_state_reflects_applied_fixes(client, library):
    """After a fix is applied, nothing should still say "Needs retiming"."""
    page = get(client, "/")
    assert "Needs retiming <b>1</b>" in page
    post(client, "/job/sw/apply", {"next": "/"})
    page = get(client, "/")
    assert "Needs retiming" not in page
    assert "Fixed <b>1</b>" in page
    job = get(client, "/job/sw")
    assert ">Fixed</span>" in job and "Talkarr found: needs retiming." in job
    assert ">Fixed</span>" in get(client, "/library")


@pytest.mark.parametrize(
    ("outcome", "verdict", "state"),
    [
        ("applied", "retimed", "fixed"),
        ("pending", "retimed", "needs_retiming"),
        ("partial", "mislabelled", "not_forced"),
        ("pending", None, "waiting"),
        ("failed", "retimed", "failed"),
        ("dismissed", "retimed", "left"),
        ("nothing", "in_sync", "in_sync"),
        ("nothing", "mislabelled", "not_forced"),
        ("nothing", None, "fine"),
        (None, None, None),
    ],
)
def test_state_of(outcome, verdict, state):
    from talkarr.ui import state_of

    assert state_of({"outcome": outcome, "verdict": verdict}) == state


def test_settings_offers_to_apply_waiting_fixes(client, library):
    assert "Apply them now" not in get(client, "/settings")  # Manual applies nothing itself
    settings.set_mode("safe_auto")
    page = get(client, "/settings")
    assert "2 subtitles have waiting fixes" in page and "Apply them now" in page
    page = html.unescape(post(client, "/settings/catch-up", {}).text)
    assert "Applying the waiting fixes on 2 subtitles" in page and "Stop" in page
    page = html.unescape(post(client, "/settings/catch-up", {"stop": "1"}).text)
    assert "Stopped." in page and "Apply them now" in page
