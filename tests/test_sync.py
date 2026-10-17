import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from talkarr import sync
from talkarr.db import SubType
from talkarr.sync import _estimate_confidence, sync_subtitle

FFSUBSYNC_LOG = (
    "INFO|ffsubsync.ffsubsync|score: 31024.000\n"
    "INFO|ffsubsync.ffsubsync|offset seconds: -2.350\n"
    "INFO|ffsubsync.ffsubsync|framerate scale factor: 1.000\n"
)


@pytest.fixture
def files(tmp_path):
    sub = tmp_path / "Movie.en.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    os.chmod(sub, 0o644)
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"\0")
    return str(sub), str(media)


@pytest.fixture
def fake_ffsubsync(monkeypatch):
    """Replace subprocess.run; the fake writes `output` to ffsubsync's -o path."""
    calls = []

    def install(
        returncode=0,
        output="1\n00:00:03,350 --> 00:00:04,350\nHi\n",
        log=FFSUBSYNC_LOG,
        raises=None,
    ):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if raises:
                raise raises
            out_path = cmd[cmd.index("-o") + 1]
            if output is not None:
                with open(out_path, "w") as f:
                    f.write(output)
            return SimpleNamespace(returncode=returncode, stdout="", stderr=log)

        monkeypatch.setattr(sync.subprocess, "run", run)
        return calls

    return install


def leftover_temp_files(directory):
    return [f for f in os.listdir(directory) if f.startswith("tmp")]


def test_forced_subs_are_never_synced(files, fake_ffsubsync):
    calls = fake_ffsubsync()
    result = sync_subtitle(*files, SubType.FORCED)
    assert result.success and result.skipped
    assert calls == []


def test_missing_subtitle(tmp_path):
    result = sync_subtitle(str(tmp_path / "x.srt"), str(tmp_path / "x.mkv"), SubType.REGULAR)
    assert not result.success
    assert "Subtitle not found" in result.error


def test_missing_media(files, tmp_path):
    result = sync_subtitle(files[0], str(tmp_path / "missing.mkv"), SubType.REGULAR)
    assert not result.success
    assert "Media not found" in result.error


def test_successful_sync_replaces_file_and_parses_log(files, fake_ffsubsync):
    sub, media = files
    calls = fake_ffsubsync()

    result = sync_subtitle(sub, media, SubType.REGULAR)

    assert result.success and not result.skipped
    assert result.offset == pytest.approx(-2.35)
    assert result.framerate == pytest.approx(1.0)
    assert result.score == pytest.approx(31024.0)
    assert result.confidence == pytest.approx(1.0)
    assert "00:00:03,350" in open(sub).read()
    assert calls[0][:2] == ["ffsubsync", media]
    assert stat.S_IMODE(os.stat(sub).st_mode) == 0o644
    assert leftover_temp_files(os.path.dirname(sub)) == []


def test_nonzero_exit_keeps_original(files, fake_ffsubsync):
    sub, media = files
    before = open(sub).read()
    fake_ffsubsync(returncode=1, log="boom")

    result = sync_subtitle(sub, media, SubType.HI)

    assert not result.success
    assert "exit 1" in result.error
    assert open(sub).read() == before
    assert leftover_temp_files(os.path.dirname(sub)) == []


def test_empty_output_keeps_original(files, fake_ffsubsync):
    sub, media = files
    before = open(sub).read()
    fake_ffsubsync(output="")

    result = sync_subtitle(sub, media, SubType.REGULAR)

    assert not result.success
    assert "empty output" in result.error
    assert open(sub).read() == before


def test_timeout(files, fake_ffsubsync):
    sub, media = files
    fake_ffsubsync(raises=subprocess.TimeoutExpired("ffsubsync", 600))

    result = sync_subtitle(sub, media, SubType.REGULAR)

    assert not result.success
    assert "timed out" in result.error
    assert leftover_temp_files(os.path.dirname(sub)) == []


@pytest.mark.parametrize(
    ("offset", "framerate", "expected"),
    [
        (None, None, 1.0),
        (0.5, 1.0, 1.0),
        (5, None, 0.9),
        (15, None, 0.7),
        (45, None, 0.5),
        (0, 1.02, 0.9),
        (0, 1.0427, 0.9),
        (0, 0.9, 0.7),
        (45, 0.9, 0.2),
    ],
)
def test_confidence(offset, framerate, expected):
    assert _estimate_confidence(offset, framerate) == pytest.approx(expected)
