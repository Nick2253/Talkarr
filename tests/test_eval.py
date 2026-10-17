import json
import random

import pytest

from talkarr.eval.__main__ import main
from talkarr.eval.library import find_items
from talkarr.eval.perturb import cases
from talkarr.forced import subtitles as S
from talkarr.forced.subtitles import Cue
from talkarr.forced.verify import Verdict
from tests.test_forced import sentence


def make_film(root, title, seed, forced_name=".en.forced.srt", with_reference=True):
    rng = random.Random(seed)
    full, t = [], 20.0
    for _ in range(250):
        full.append(Cue(t, t + 2.0, f"{title.split()[0]} {sentence(rng)}"))
        t += rng.uniform(3, 25)
    forced = [full[i] for i in sorted(rng.sample(range(250), 20))]
    d = root / "movies" / title
    d.mkdir(parents=True)
    stem = f"{title} [Bluray-1080p]"
    (d / f"{stem}{forced_name}").write_text(S.compose(forced))
    if with_reference:
        (d / f"{stem}.en.srt").write_text(S.compose(full))
    (d / f"{stem}.mkv").write_bytes(b"")
    return d


@pytest.fixture
def library(tmp_path):
    make_film(tmp_path, "Alpha (2001)", 1)
    make_film(tmp_path, "Bravo (2002)", 2)
    make_film(tmp_path, "Charlie (2003)", 3, with_reference=False)
    make_film(tmp_path, "Delta (2004)", 4, forced_name=".fr.forced.srt")
    return tmp_path


def test_find_items_pairs_english_forced_with_reference(library):
    items = {i.title: i for i in find_items(str(library / "movies"))}
    assert set(items) == {"Alpha (2001)", "Bravo (2002)", "Charlie (2003)"}
    assert items["Alpha (2001)"].reference.endswith("Alpha (2001) [Bluray-1080p].en.srt")
    assert items["Charlie (2003)"].reference is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Movie.en.forced.srt", "Movie"),
        ("Movie.eng.FORCED.srt", "Movie"),
        ("Movie.english.foreign.srt", "Movie"),
        ("Movie.en.srt", None),
        ("Movie.de.forced.srt", None),
        ("Movie.en.forced.ass", None),
    ],
)
def test_english_forced_detection(tmp_path, name, expected):
    (tmp_path / name).write_text("")
    items = find_items(str(tmp_path))
    assert ([i.forced.rsplit("/", 1)[1] for i in items] == [name]) == (expected is not None)


def test_perturbation_cases_cover_every_verdict():
    forced = [Cue(100.0 * i, 100.0 * i + 2, f"line {i}") for i in range(1, 11)]
    got = cases(forced, forced * 30, other_forced=forced)
    assert {c.expected for c in got} == {
        Verdict.IN_SYNC,
        Verdict.RETIMED,
        Verdict.MISLABELLED,
        Verdict.MISMATCH,
    }
    assert all(c.cues for c in got)


def test_sweep_cli(library, tmp_path, capsys):
    out = tmp_path / "lab"
    assert main(["sweep", "--root", str(library / "movies"), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| in_sync | 2 |" in printed
    assert "| unverifiable | 1 |" in printed
    (result,) = (out / "runs").iterdir()
    rows = json.loads(result.read_text())["rows"]
    assert {r["verdict"] for r in rows} == {"in_sync", "unverifiable"}


def test_perturb_cli_passes_on_synthetic_library(library, capsys):
    assert main(["perturb", "--root", str(library / "movies")]) == 0
    printed = capsys.readouterr().out
    assert "perturbation corpus: 2 films" in printed
    assert "failures" not in printed
    assert "**34/34 passed**" in printed


def test_title_filter_and_bad_files(library, capsys):
    bad = library / "movies" / "Alpha (2001)" / "Alpha (2001) [Bluray-1080p].en.forced.srt"
    bad.write_bytes(b"\xff\xfe\x00garbage")
    assert main(["sweep", "--root", str(library / "movies"), "--title", "alpha"]) == 0
    printed = capsys.readouterr().out
    assert "1 English forced subtitles" in printed


def test_sweep_with_reference_sync_uses_synced_reference(library, tmp_path, monkeypatch, capsys):
    """The synced full subtitle (here: shifted 3 s by a fake ffsubsync) is what the
    forced subtitle is judged against; results are cached across runs."""
    from talkarr.eval import refsync

    calls = []

    def fake_ffsubsync(subtitle_path, media_path, output_path, **_):
        calls.append(subtitle_path)
        shifted = S.transform(S.load(subtitle_path), offset=3.0)
        with open(output_path, "w") as f:
            f.write(S.compose(shifted))
        from types import SimpleNamespace

        return SimpleNamespace(success=True, offset=3.0, framerate=1.0, score=1.0, error=None)

    monkeypatch.setattr(refsync, "run_ffsubsync", fake_ffsubsync)
    out = tmp_path / "lab"
    args = ["sweep", "--root", str(library / "movies"), "--out", str(out), "--sync-reference"]

    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "| retimed | 2 |" in printed
    assert "full sub ffsubsync offset +3.00s" in printed
    assert len(calls) == 2

    assert main(args) == 0  # second run: served from cache
    assert len(calls) == 2


def test_reference_sync_failure_keeps_unsynced_verdict(library, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from talkarr.eval import refsync

    monkeypatch.setattr(
        refsync,
        "run_ffsubsync",
        lambda *a, **k: SimpleNamespace(
            success=False, offset=None, framerate=None, score=None, error="no audio"
        ),
    )
    args = [
        "sweep",
        "--root",
        str(library / "movies"),
        "--out",
        str(tmp_path / "lab"),
        "--sync-reference",
    ]
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "| in_sync | 2 |" in printed
    assert "full sub sync failed: no audio" in printed


def test_failed_reference_sync_is_retried(library, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from talkarr.eval import refsync
    from talkarr.eval.library import find_items

    item = next(i for i in find_items(str(library / "movies")) if i.reference)
    calls = []

    def failing(*a, **k):
        calls.append(1)
        return SimpleNamespace(success=False, offset=None, framerate=None, score=None, error="x")

    monkeypatch.setattr(refsync, "run_ffsubsync", failing)
    cache = str(tmp_path / "cache")
    assert refsync.synced_reference(item, cache)[0] is None
    assert refsync.synced_reference(item, cache)[0] is None
    assert len(calls) == 2


def test_audio_cli_with_fake_analysis(library, monkeypatch, capsys):
    """Foreign speech exactly where each film's forced cues are: every perturbation
    must be detected and repaired by the audio path alone."""
    from talkarr.eval import __main__ as cli
    from talkarr.forced.audio import Region

    def fake_regions(item, cache_dir, ml_url):
        cues = S.load(item.forced)
        regions = [
            Region(c.start - 0.1, c.end + 0.1, 0.02, (("ru", 0.8), ("uk", 0.1))) for c in cues
        ]
        info = {
            "ok": True,
            "stream": {"index": 1, "layout": "5.1", "center": True, "language": "eng"},
            "regions": len(regions),
        }
        return regions, info

    monkeypatch.setattr(cli, "film_regions", fake_regions)
    assert main(["audio", "--root", str(library / "movies")]) == 0
    printed = capsys.readouterr().out
    assert "audio corpus: 2 films" in printed
    assert "#1 5.1 FC" in printed
    assert "failures" not in printed, printed


def test_audio_cli_reports_analysis_errors(library, monkeypatch, capsys):
    from talkarr.eval import __main__ as cli

    def broken(*a, **k):
        raise ConnectionError("talkarr-ml unreachable")

    monkeypatch.setattr(cli, "film_regions", broken)
    assert main(["audio", "--root", str(library / "movies")]) == 0
    assert "talkarr-ml unreachable" in capsys.readouterr().out
