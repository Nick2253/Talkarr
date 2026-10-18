import pytest

from talkarr import db, paths
from talkarr.config import Config


def srt_cue(index, start_s, end_s, text):
    """One SRT cue. Times are in seconds."""
    return f"{index}\n{_ts(start_s)} --> {_ts(end_s)}\n{text}\n"


def make_srt(texts, start=10.0, duration=2.0, gap=1.0):
    """An SRT document with one cue per text, evenly spaced."""
    cues = []
    t = start
    for i, text in enumerate(texts, 1):
        cues.append(srt_cue(i, t, t + duration, text))
        t += duration + gap
    return "\n".join(cues)


def _ts(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Every test gets its own database and default settings."""
    monkeypatch.setattr(Config, "DATABASE_PATH", str(tmp_path / "talkarr.db"))
    monkeypatch.setattr(Config, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(Config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(Config, "PATH_MAPPING", "")
    monkeypatch.setattr(Config, "MEDIA_PATH", str(tmp_path / "media"))
    monkeypatch.setattr(Config, "SYNC_ENABLED", True)
    monkeypatch.setattr(Config, "CLEAN_ENABLED", True)
    monkeypatch.setattr(Config, "MODE", "manual")
    monkeypatch.setattr(Config, "MISLABELLED_ACTION", "rename_or_remove")
    monkeypatch.setattr(Config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(Config, "FORCED_VERIFY_ENABLED", False)
    monkeypatch.setattr(Config, "ML_URL", "")
    monkeypatch.setattr(Config, "CACHE_DIR", str(tmp_path / "cache"))
    db._local.db = None
    paths._mappings = None
    db.init_db()
    yield
    if getattr(db._local, "db", None) is not None:
        db._local.db.close()
        db._local.db = None
    paths._mappings = None


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def client():
    from talkarr.app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
