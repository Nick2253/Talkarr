import json
import time
import urllib.parse

import pytest
import responses

from talkarr import bazarr, db, fixes, paths, review
from talkarr.config import Config
from tests.conftest import make_srt

URL = "http://bazarr:6767"
MOVIE_SUB = "/data/movies/Man on Fire (2004)/Man on Fire (2004).en.forced.srt"
MOVIE_VIDEO = "/data/movies/Man on Fire (2004)/Man on Fire (2004).mkv"


@pytest.fixture(autouse=True)
def bazarr_config(monkeypatch):
    monkeypatch.setattr(Config, "BAZARR_URL", URL)
    monkeypatch.setattr(Config, "BAZARR_API_KEY", "k")
    monkeypatch.setattr(Config, "BAZARR_BLACKLIST", True)
    monkeypatch.setattr(Config, "PATH_MAPPING", "/data:/media")
    paths._mappings = None


def history_row(path, subs_id="10092957", ts=100, blacklisted=False, forced=True):
    return {
        "subtitles_path": path,
        "provider": "subsource",
        "subs_id": subs_id,
        "timestamp": ts,
        "blacklisted": blacklisted,
        "language": {
            "name": "English",
            "code2": "en",
            "code3": "eng",
            "forced": forced,
            "hi": False,
        },
    }


def movies(rows=None):
    responses.get(
        f"{URL}/api/movies",
        json={"data": [{"radarrId": 280, "title": "Man on Fire", "path": MOVIE_VIDEO}]},
    )
    responses.get(
        f"{URL}/api/movies/history",
        json={
            "data": rows
            if rows is not None
            else [
                history_row(MOVIE_SUB, "older", ts=50),
                history_row(MOVIE_SUB, ts=100),
                history_row(MOVIE_SUB.replace(".forced", ""), "regular", ts=200, forced=False),
            ]
        },
    )


def test_to_source_path():
    assert paths.to_source_path("/media/movies/a.srt") == "/data/movies/a.srt"
    assert paths.to_source_path("/mediax/a.srt") == "/mediax/a.srt"
    assert paths.to_source_path("") == ""


def test_enabled_needs_key_and_flag(monkeypatch):
    assert bazarr.enabled()
    monkeypatch.setattr(Config, "BAZARR_API_KEY", "")
    assert not bazarr.enabled()


@responses.activate
def test_find_download_for_a_movie():
    movies()
    d = bazarr.Bazarr().find_download(
        MOVIE_SUB.replace("/data", "/media"), MOVIE_VIDEO.replace("/data", "/media")
    )
    assert d == bazarr.Download(
        "movie", {"radarrid": 280}, "subsource", "10092957", "en", MOVIE_SUB
    )
    assert "radarrid=280" in responses.calls[1].request.url


@responses.activate
def test_an_already_blacklisted_download_is_marked():
    movies([history_row(MOVIE_SUB, blacklisted=True)])
    d = bazarr.Bazarr().find_download(MOVIE_SUB.replace("/data", "/media"), None)
    assert d.blacklisted and d.subs_id == "10092957"


@responses.activate
def test_find_download_for_an_episode():
    responses.get(f"{URL}/api/movies", json={"data": []})
    responses.get(
        f"{URL}/api/series", json={"data": [{"sonarrSeriesId": 7, "path": "/data/tv/Barry"}]}
    )
    video = "/data/tv/Barry/Season 01/Barry - S01E08.mkv"
    sub = "/data/tv/Barry/Season 01/Barry - S01E08.en.forced.srt"
    responses.get(f"{URL}/api/episodes", json={"data": [{"sonarrEpisodeId": 70, "path": video}]})
    responses.get(f"{URL}/api/episodes/history", json={"data": [history_row(sub, "abc")]})
    d = bazarr.Bazarr().find_download(
        sub.replace("/data", "/media"), video.replace("/data", "/media")
    )
    assert d.kind == "episode" and d.ids == {"seriesid": 7, "episodeid": 70} and d.subs_id == "abc"


@responses.activate
def test_unknown_media_and_http_errors():
    responses.get(f"{URL}/api/movies", json={"data": []})
    responses.get(f"{URL}/api/series", json={"data": []})
    assert bazarr.Bazarr().find_download("/media/x/y.srt", None) is None
    responses.replace(responses.GET, f"{URL}/api/movies", status=500)
    with pytest.raises(bazarr.BazarrError):
        bazarr.Bazarr().find_download("/media/x/y.srt", None)


@responses.activate
def test_blacklist_posts_bazarrs_form():
    responses.post(f"{URL}/api/movies/blacklist", status=200)
    d = bazarr.Download("movie", {"radarrid": 280}, "subsource", "1", "en", MOVIE_SUB)
    bazarr.Bazarr().blacklist(d)
    form = dict(urllib.parse.parse_qsl(responses.calls[0].request.body))
    assert form == {
        "radarrid": "280",
        "provider": "subsource",
        "subs_id": "1",
        "language": "en",
        "subtitles_path": MOVIE_SUB,
    }
    assert responses.calls[0].request.headers["X-API-KEY"] == "k"
    responses.replace(
        responses.POST, f"{URL}/api/movies/blacklist", status=500, body="Subtitles file not found"
    )
    with pytest.raises(bazarr.BazarrError, match="refused"):
        bazarr.Bazarr().blacklist(d)


# --- removing through the fix engine ------------------------------------------------------


@pytest.fixture
def removal(media_dir, monkeypatch):
    monkeypatch.setattr(Config, "MEDIA_PATH", str(media_dir))
    folder = media_dir / "movies" / "Man on Fire (2004)"
    folder.mkdir(parents=True)
    sub = folder / "Man on Fire (2004).en.forced.srt"
    sub.write_text(make_srt([f"line {i}" for i in range(250)]))
    (folder / "Man on Fire (2004).mkv").write_bytes(b"")
    monkeypatch.setattr(Config, "PATH_MAPPING", f"/data:{media_dir}")
    paths._mappings = None

    def make(backup):
        fix = fixes._fix("remove", "Remove", "confident", {"backup": backup})
        db.create_job("j", str(sub), str(folder / "Man on Fire (2004).mkv"), "en")
        db.update_job_proposal(
            "j", {"fingerprint": fixes.fingerprint(str(sub)), "fixes": [fix]}, "pending"
        )
        return sub

    return make


def bazarr_paths(sub):
    return str(sub).replace(Config.PATH_MAPPING.split(":")[1], "/data")


@responses.activate
def test_remove_without_backup_goes_through_bazarr(removal):
    sub = removal(backup=False)
    responses.get(
        f"{URL}/api/movies",
        json={
            "data": [
                {"radarrId": 280, "path": bazarr_paths(sub.with_name("Man on Fire (2004).mkv"))}
            ]
        },
    )
    responses.get(f"{URL}/api/movies/history", json={"data": [history_row(bazarr_paths(sub))]})

    def bazarr_deletes(request):
        sub.unlink()
        return (200, {}, "")

    responses.add_callback(responses.POST, f"{URL}/api/movies/blacklist", callback=bazarr_deletes)

    applied = review.apply_job("j")
    assert not sub.exists()
    assert applied[0]["result"].startswith("Blacklisted in Bazarr (subsource #10092957)")
    history = json.loads(db.get_job("j")["backup"])
    assert history[0]["copy"] is None and history[0]["bazarr"]["subs_id"] == "10092957"
    with pytest.raises(review.ReviewError, match="removed without a backup"):
        review.revert_job("j")


@responses.activate
def test_remove_with_backup_and_no_bazarr_record(removal):
    sub = removal(backup=True)
    responses.get(f"{URL}/api/movies", json={"data": []})
    responses.get(f"{URL}/api/series", json={"data": []})
    applied = review.apply_job("j")
    assert (
        applied[0]["result"]
        == "Bazarr has no record of downloading it, so it was deleted directly."
    )
    assert not sub.exists()
    review.revert_job("j")
    assert sub.exists()


@responses.activate
def test_bazarr_unreachable_leaves_the_file_and_retries_later(removal, monkeypatch):
    from talkarr import settings

    sub = removal(backup=False)
    settings.set_mode("full_auto")
    responses.get(f"{URL}/api/movies", status=502)
    with pytest.raises(review.ReviewError, match="couldn't reach Bazarr.*retry"):
        review.apply_job("j")
    assert sub.exists()
    [fix] = json.loads(db.get_job("j")["proposal"])["fixes"]
    assert fix["state"] == "proposed" and fix["retry_after"] > time.time()
    assert db.get_job("j")["outcome"] == "pending"
    db.update_job_status("j", "completed")

    assert review.waiting_for("full_auto") == []  # not due yet: catch-up leaves it
    later = time.time() + fixes.RETRY_SECONDS + 1
    review._last_retry_scan[0] = 0.0
    responses.replace(responses.GET, f"{URL}/api/movies", json={"data": []})
    responses.get(f"{URL}/api/series", json={"data": []})
    assert review.retry_step(clock=lambda: later)
    assert not sub.exists() and db.get_job("j")["outcome"] == "applied"


def test_remove_without_bazarr_just_deletes(removal, monkeypatch):
    monkeypatch.setattr(Config, "BAZARR_BLACKLIST", False)
    sub = removal(backup=False)
    assert review.apply_job("j")[0]["result"] == "Deleted."
    assert not sub.exists()


def test_ui_for_a_removal_without_backup(client, removal, monkeypatch):
    import html

    monkeypatch.setattr(Config, "BAZARR_BLACKLIST", False)
    removal(backup=False)
    page = html.unescape(client.get("/").data.decode())
    assert 'data-confirm="Remove permanently?"' in page
    job = html.unescape(client.get("/job/j").data.decode())
    assert "No backup is kept." in job
    client.post("/job/j/apply", headers={"Origin": "http://localhost"})
    job = html.unescape(client.get("/job/j").data.decode())
    assert "You removed the file; no backup kept." in job
    assert "Restore the original" not in job


@responses.activate
def test_remove_already_blacklisted_deletes_directly(removal):
    sub = removal(backup=False)
    responses.get(
        f"{URL}/api/movies",
        json={
            "data": [
                {"radarrId": 280, "path": bazarr_paths(sub.with_name("Man on Fire (2004).mkv"))}
            ]
        },
    )
    responses.get(
        f"{URL}/api/movies/history",
        json={"data": [history_row(bazarr_paths(sub), blacklisted=True)]},
    )
    applied = review.apply_job("j")
    assert not sub.exists()
    assert applied[0]["result"].startswith("Already blacklisted in Bazarr (subsource #10092957)")


def replacement(removal):
    sub = removal(backup=True)
    proposal = json.loads(db.get_job("j")["proposal"])
    proposal["fixes"][0]["params"]["replace"] = True
    db.update_job_proposal("j", proposal, "pending")
    return sub


@responses.activate
def test_replace_goes_through_bazarr(removal):
    sub = replacement(removal)
    responses.get(
        f"{URL}/api/movies",
        json={
            "data": [
                {"radarrId": 280, "path": bazarr_paths(sub.with_name("Man on Fire (2004).mkv"))}
            ]
        },
    )
    responses.get(f"{URL}/api/movies/history", json={"data": [history_row(bazarr_paths(sub))]})
    responses.post(f"{URL}/api/movies/blacklist")
    applied = review.apply_job("j")
    assert applied[0]["result"].startswith("Blacklisted in Bazarr")
    assert not sub.exists()


@responses.activate
def test_replace_leaves_a_subtitle_bazarr_did_not_download(removal):
    sub = replacement(removal)
    responses.get(f"{URL}/api/movies", json={"data": []})
    responses.get(f"{URL}/api/series", json={"data": []})
    with pytest.raises(review.ReviewError, match="nothing would replace it"):
        review.apply_job("j")
    assert sub.exists()
