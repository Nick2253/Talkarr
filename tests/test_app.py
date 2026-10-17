import pytest

from talkarr import db
from talkarr.app import _extract_language, _find_media_for_subtitle
from talkarr.config import Config
from talkarr.db import JobStatus


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


# --- health / dashboard / read endpoints ------------------------------------


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_dashboard_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"No jobs yet" in r.data


def test_dashboard_lists_jobs_and_escapes_paths(client):
    db.create_job("j1", "/m/<script>alert(1)</script>.srt", "/m/a.mkv", "en")
    db.update_job_validation("j1", 0.55, "x")
    db.update_job_sync("j1", -1.5, 0.8)
    r = client.get("/")
    assert b"<script>alert(1)</script>" not in r.data
    assert b"&lt;script&gt;" in r.data
    assert b"55%" in r.data and b"-1.50s" in r.data


def test_jobs_and_stats(client):
    db.create_job("j1", "/m/a.srt", "/m/a.mkv", "en")
    assert [j["id"] for j in client.get("/jobs").get_json()] == ["j1"]
    assert client.get("/stats").get_json() == {"pending": 1, "total": 1}


# --- /process ----------------------------------------------------------------


def test_process_queues_job(client):
    r = client.post(
        "/process", json={"subtitle": "/m/a.en.srt", "media": "/m/a.mkv", "language": "en"}
    )
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "queued"
    job = db.get_job(body["job_id"])
    assert job["subtitle_path"] == "/m/a.en.srt"
    assert job["media_path"] == "/m/a.mkv"


def test_process_translates_paths(client, monkeypatch):
    monkeypatch.setattr(Config, "PATH_MAPPING", "/data:/media")
    r = client.post("/process", json={"subtitle": "/data/a.srt", "media": "/data/a.mkv"})
    job = db.get_job(r.get_json()["job_id"])
    assert job["subtitle_path"] == "/media/a.srt"
    assert job["media_path"] == "/media/a.mkv"


@pytest.mark.parametrize("payload", [None, {}, {"media": "/m/a.mkv"}])
def test_process_rejects_bad_payload(client, payload):
    r = client.post("/process", json=payload) if payload is not None else client.post("/process")
    assert r.status_code == 400


def test_process_deduplicates_in_flight_jobs(client):
    first = client.post("/process", json={"subtitle": "/m/a.srt"}).get_json()
    second = client.post("/process", json={"subtitle": "/m/a.srt"}).get_json()
    assert second == {"status": "duplicate", "job_id": first["job_id"]}


def test_process_requeues_finished_subtitle(client):
    first = client.post("/process", json={"subtitle": "/m/a.srt"}).get_json()
    db.update_job_status(first["job_id"], JobStatus.COMPLETED)
    second = client.post("/process", json={"subtitle": "/m/a.srt"}).get_json()
    assert second["status"] == "queued" and second["job_id"] != first["job_id"]


def test_process_requires_secret_when_configured(client, monkeypatch):
    monkeypatch.setattr(Config, "WEBHOOK_SECRET", "s3cret")
    payload = {"subtitle": "/m/a.srt"}
    assert client.post("/process", json=payload).status_code == 401
    assert (
        client.post("/process", json=payload, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            "/process", json=payload, headers={"Authorization": "Bearer s3cret"}
        ).status_code
        == 200
    )


# --- /bulk ---------------------------------------------------------------------


@pytest.fixture
def library(media_dir):
    movie = media_dir / "movies" / "Movie (2020)"
    touch(movie / "Movie (2020).mkv")
    touch(movie / "Movie (2020).en.srt")
    touch(movie / "Movie (2020).en.forced.srt")
    touch(movie / "Movie (2020).en.hi.srt")
    season = media_dir / "tv" / "Show" / "Season 01"
    for ep in (1, 2):
        touch(season / f"Show - S01E0{ep}.mkv")
        touch(season / f"Show - S01E0{ep}.en.srt")
    touch(season / "Orphan.en.srt")  # no matching video, folder has two videos
    return media_dir


def bulk(client, library, **kwargs):
    r = client.post("/bulk", json={"directory": str(library), **kwargs})
    assert r.status_code == 200
    return r.get_json()["count"]


def test_bulk_queues_all_pairable_subtitles(client, library):
    assert bulk(client, library) == 5
    pairs = {
        (j["subtitle_path"].rsplit("/", 1)[1], j["media_path"].rsplit("/", 1)[1])
        for j in db.get_recent_jobs()
    }
    assert ("Show - S01E02.en.srt", "Show - S01E02.mkv") in pairs
    assert all(j["source"] == "bulk" for j in db.get_recent_jobs())


@pytest.mark.parametrize(("flt", "count"), [("forced", 1), ("hi", 1), ("regular", 3)])
def test_bulk_filters(client, library, flt, count):
    assert bulk(client, library, filter=flt) == count


def test_bulk_skips_completed(client, library):
    bulk(client, library)
    for job in db.get_recent_jobs():
        db.update_job_status(job["id"], JobStatus.COMPLETED)
    assert bulk(client, library) == 0


def test_bulk_rejects_missing_directory(client, tmp_path):
    r = client.post("/bulk", json={"directory": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_bulk_requires_payload(client):
    assert client.post("/bulk").status_code == 400


# --- helpers ---------------------------------------------------------------------


def test_media_pairing_uses_full_stem(tmp_path):
    touch(tmp_path / "Movie.Name.2020.mkv")
    touch(tmp_path / "Movie.mkv")
    sub = touch(tmp_path / "Movie.Name.2020.en.forced.srt")
    assert _find_media_for_subtitle(str(sub)) == str(tmp_path / "Movie.Name.2020.mkv")


def test_media_pairing_never_guesses_between_episodes(tmp_path):
    touch(tmp_path / "S01E01.mkv")
    touch(tmp_path / "S01E02.mkv")
    sub = touch(tmp_path / "Something.else.en.srt")
    assert _find_media_for_subtitle(str(sub)) is None


def test_media_pairing_single_video_fallback(tmp_path):
    touch(tmp_path / "Movie.2020.1080p.MKV")
    sub = touch(tmp_path / "movie.en.srt")
    assert _find_media_for_subtitle(str(sub)) == str(tmp_path / "Movie.2020.1080p.MKV")


@pytest.mark.parametrize(
    ("name", "lang"),
    [
        ("Movie.en.srt", "en"),
        ("Movie.eng.forced.srt", "en"),
        ("Movie.english.hi.srt", "en"),
        ("Movie.fr.srt", "fr"),
        ("Movie.por.sdh.srt", "por"),
        ("Movie.srt", ""),
    ],
)
def test_extract_language(name, lang):
    assert _extract_language(name) == lang
