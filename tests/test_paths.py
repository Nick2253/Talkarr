import pytest

from talkarr import paths
from talkarr.config import Config


@pytest.fixture
def mapping(monkeypatch):
    def _set(value):
        monkeypatch.setattr(Config, "PATH_MAPPING", value)
        paths._mappings = None

    return _set


def test_no_mapping_is_identity(mapping):
    mapping("")
    assert paths.translate_path("/data/movies/a.srt") == "/data/movies/a.srt"


def test_single_mapping(mapping):
    mapping("/data:/media")
    assert paths.translate_path("/data/movies/a.srt") == "/media/movies/a.srt"


def test_mapping_matches_whole_segments_only(mapping):
    mapping("/data:/media")
    assert paths.translate_path("/database/x.srt") == "/database/x.srt"
    assert paths.translate_path("/data") == "/media"


def test_trailing_slashes_are_normalised(mapping):
    mapping("/data/:/media/")
    assert paths.translate_path("/data/tv/b.srt") == "/media/tv/b.srt"


def test_multiple_mappings_first_match_wins(mapping):
    mapping("/data/tv:/tv | /data:/media | garbage")
    assert paths.translate_path("/data/tv/s.srt") == "/tv/s.srt"
    assert paths.translate_path("/data/movies/m.srt") == "/media/movies/m.srt"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_paths_pass_through(mapping, value):
    mapping("/data:/media")
    assert paths.translate_path(value) == value
