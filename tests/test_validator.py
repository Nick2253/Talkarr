import pytest

from talkarr.db import SubType
from talkarr.validator import validate
from tests.conftest import make_srt, srt_cue


def write(tmp_path, text, name="a.srt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def lines(n):
    return [f"line {i}" for i in range(n)]


def test_missing_file(tmp_path):
    result = validate(str(tmp_path / "x.srt"), SubType.REGULAR)
    assert not result.passed
    assert result.issues == ["File not found"]


def test_empty_file(tmp_path):
    result = validate(write(tmp_path, ""), SubType.REGULAR)
    assert not result.passed
    assert "empty" in result.issues[0]


def test_good_regular_sub_passes(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(300))), SubType.REGULAR)
    assert result.passed
    assert result.score == pytest.approx(1.0)
    assert result.issues == [] and result.warnings == []


def test_good_forced_sub_passes(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(25), gap=600)), SubType.FORCED)
    assert result.passed
    assert result.warnings == []


def test_mislabelled_forced_sub_is_an_issue(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(250))), SubType.FORCED)
    assert not result.passed
    assert any("mislabeled" in i.lower() for i in result.issues)


def test_forced_sub_slightly_dense_only_warns(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(150))), SubType.FORCED)
    assert result.passed
    assert len(result.warnings) == 1


def test_sparse_regular_sub_warns(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(3))), SubType.REGULAR)
    assert len(result.warnings) == 2  # under 20 cues, and under 5 cues


def test_short_and_long_cues_warn(tmp_path):
    cues = [srt_cue(i + 1, i * 40, i * 40 + 0.1, "x") for i in range(30)]
    cues += [srt_cue(31 + i, 2000 + i * 40, 2000 + i * 40 + 35, "y") for i in range(5)]
    result = validate(write(tmp_path, "\n".join(cues)), SubType.REGULAR)
    assert any("shorter than" in w for w in result.warnings)
    assert any("longer than" in w for w in result.warnings)


def test_large_gaps_in_regular_sub_warn(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(30), gap=700)), SubType.REGULAR)
    assert any("gaps longer" in w for w in result.warnings)


def test_low_sync_confidence_warns(tmp_path):
    result = validate(write(tmp_path, make_srt(lines(300))), SubType.REGULAR, sync_confidence=0.2)
    assert any("sync confidence" in w for w in result.warnings)
    assert result.score == pytest.approx(0.8)


def test_non_utf8_file_still_parses(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes(make_srt(["Très bien"] * 30).encode("cp1252"))
    assert validate(str(p), SubType.REGULAR).passed
