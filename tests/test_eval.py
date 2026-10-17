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
