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


def test_measure_correction_is_exact_where_the_log_is_rounded(tmp_path):
    """ffsubsync logs "framerate scale factor: 0.959" for 23.976/25: rebuilding the
    correction from that is up to 1.5 s off by the end of a film."""
    from talkarr.forced import subtitles as S
    from tests.conftest import make_srt

    before = tmp_path / "in.srt"
    before.write_text(make_srt([f"line {i}" for i in range(400)], start=30.0, gap=15.0))
    after = tmp_path / "out.srt"
    after.write_text(S.compose(S.transform(S.load(before), scale=23.976 / 25, offset=-0.9)))
    scale, offset = sync.measure_correction(str(before), str(after))
    assert scale == pytest.approx(23.976 / 25, abs=1e-6) and offset == pytest.approx(-0.9, abs=2e-3)

    after.write_text(S.compose(S.load(before)[:-1]))
    assert sync.measure_correction(str(before), str(after)) is None


# 90 cues 17 s apart: the thirds' cues average 256.5 s, 766.5 s and 1276.5 s.
X1001 = (1.001, 0.36 - 0.001 * 256.5)  # predicts +0.36, +0.87, +1.38


@pytest.mark.parametrize(
    ("correction", "parts", "expected"),
    [
        (X1001, (0.36, 0.85, 1.18), True),  # Futurama S05E04
        (X1001, (0.63, 0.5, 0.11), False),  # S03E03: the ends disagree with the drift
        (X1001, (0.30, 0.30, 0.30), False),  # no drift at all
        ((1.0, -89.09), (-89.09, -89.09, -89.09), True),  # The Good Place
        ((1.0, -119.98), (77.65, -119.98, -119.91), False),  # South Park: another cut
        (X1001, None, None),  # a check sync failed
    ],
)
def test_confirm_sync(tmp_path, monkeypatch, correction, parts, expected):
    from tests.conftest import make_srt

    sub = tmp_path / "Show.en.srt"
    sub.write_text(make_srt([f"line {i}" for i in range(90)], start=10.0, gap=15.0))
    offsets = iter(parts or ())
    calls = []

    def run(src, reference, out, fix_framerate=True, max_offset=120, **_):
        calls.append((reference, fix_framerate, max_offset))
        o = next(offsets, None)
        return sync.SyncResult(success=o is not None, offset=o)

    monkeypatch.setattr(sync, "run_ffsubsync", run)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scale, offset = correction
    assert sync.confirm_sync(str(sub), "film.npz", scale, offset, str(scratch)) is expected
    assert {c[:2] for c in calls} == {("film.npz", False)}
    assert len(calls) == (3 if parts else 1)
    assert os.listdir(scratch) == []


def test_confirm_sync_searches_as_far_as_a_framerate_change_reaches(tmp_path, monkeypatch):
    from tests.conftest import make_srt

    sub = tmp_path / "Film.en.srt"
    sub.write_text(make_srt([f"line {i}" for i in range(300)], start=10.0, gap=20.0))
    windows = []

    def run(src, reference, out, max_offset=120, **_):
        windows.append(max_offset)
        return sync.SyncResult(success=False)

    monkeypatch.setattr(sync, "run_ffsubsync", run)
    sync.confirm_sync(str(sub), "f.npz", 23.976 / 25, 0.0, str(tmp_path))
    assert windows[0] > 250  # the last third sits ~230 s off at 25 -> 23.976


def test_no_fix_framerate_flag(files, fake_ffsubsync, tmp_path):
    calls = fake_ffsubsync()
    sync.run_ffsubsync(files[0], files[1], str(tmp_path / "o.srt"), fix_framerate=False)
    assert "--no-fix-framerate" in calls[0]


@pytest.mark.parametrize(
    ("scale", "changed"), [(1.0, False), (1.0001, False), (1.000999, True), (0.959, True)]
)
def test_changes_framerate(scale, changed):
    assert sync.changes_framerate(scale) is changed


def test_parts_offset():
    assert sync.parts_offset([(0, 0, -2.0), (0, 0, -2.2), (0, 0, -1.9)]) == -2.033
    assert sync.parts_offset([(0, 0, 0.0), (0, 0, 0.7), (0, 0, 0.1)]) is None
    assert sync.parts_offset(None) is None
