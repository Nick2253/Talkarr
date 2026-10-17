import os
import stat

import pysrt
import pytest

from talkarr.cleaner import clean_subtitle, decode_subtitle
from tests.conftest import make_srt

DIALOGUE = [f"Line number {i} of the dialogue." for i in range(1, 21)]


def write(path, text, encoding="utf-8", bom=False):
    data = text.encode(encoding)
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return str(path)


def texts(path):
    return [s.text for s in pysrt.open(path, encoding="utf-8")]


def test_missing_file(tmp_path):
    result = clean_subtitle(str(tmp_path / "nope.srt"))
    assert not result.success
    assert "not found" in result.error


def test_clean_file_is_untouched(tmp_path):
    p = tmp_path / "a.en.srt"
    write(p, make_srt(DIALOGUE))
    before = p.read_bytes()
    mtime = os.stat(p).st_mtime_ns

    result = clean_subtitle(str(p))

    assert result.success and not result.changed
    assert p.read_bytes() == before
    assert os.stat(p).st_mtime_ns == mtime


def test_bom_is_stripped(tmp_path):
    p = tmp_path / "a.srt"
    write(p, make_srt(DIALOGUE), bom=True)

    result = clean_subtitle(str(p))

    assert result.bom_stripped and result.changed
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert texts(str(p)) == DIALOGUE


def test_cp1252_file_is_rewritten_as_utf8(tmp_path):
    lines = DIALOGUE[:-1] + ["Café crème, s’il vous plaît — merci."]
    p = tmp_path / "a.srt"
    write(p, make_srt(lines), encoding="cp1252")

    result = clean_subtitle(str(p))

    assert result.success and result.reencoded and result.changed
    p.read_bytes().decode("utf-8")  # raises if not UTF-8
    assert texts(str(p))[-1] == "Café crème, s’il vous plaît — merci."


def test_double_encoded_utf8_is_repaired(tmp_path):
    good = DIALOGUE[:-2] + ["It’s cold this morning, Captain.", "Café"]
    mojibake = make_srt(good).encode("utf-8").decode("cp1252")
    p = tmp_path / "a.srt"
    write(p, mojibake)

    result = clean_subtitle(str(p))

    assert result.encoding_fixed
    assert texts(str(p))[-2:] == ["It’s cold this morning, Captain.", "Café"]


def test_legitimate_accented_text_is_not_mangled(tmp_path):
    lines = DIALOGUE[:-2] + ["São Paulo, Ångström.", "Âge tendre"]
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))

    result = clean_subtitle(str(p))

    assert not result.encoding_fixed and not result.changed
    assert texts(str(p))[-2:] == ["São Paulo, Ångström.", "Âge tendre"]


def test_strong_ads_removed_anywhere(tmp_path):
    lines = list(DIALOGUE)
    lines[10] = "Support us and become VIP member"
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))

    result = clean_subtitle(str(p))

    assert result.ads_removed == 1
    assert "Support us and become VIP member" not in texts(str(p))
    assert len(texts(str(p))) == len(DIALOGUE) - 1


def test_credits_removed_only_near_edges(tmp_path):
    lines = list(DIALOGUE)
    lines[0] = "Subtitles by SomeGroup"
    lines[-1] = "www.example.com"
    lines[10] = "Check out www.example.com, he said."
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))

    result = clean_subtitle(str(p))

    remaining = texts(str(p))
    assert result.ads_removed == 2
    assert "Subtitles by SomeGroup" not in remaining
    assert "www.example.com" not in remaining
    assert "Check out www.example.com, he said." in remaining


def test_music_cues_are_kept(tmp_path):
    lines = ["♪ ♪"] + DIALOGUE + ["♪"]
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))

    result = clean_subtitle(str(p))

    assert result.ads_removed == 0
    assert texts(str(p)) == lines


def test_refuses_to_strip_too_many_cues(tmp_path):
    lines = ["opensubtitles.org"] * 5 + DIALOGUE[:5]
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))
    before = p.read_bytes()

    result = clean_subtitle(str(p))

    assert not result.success
    assert "Refusing" in result.error
    assert p.read_bytes() == before


def test_cue_indexes_are_renumbered(tmp_path):
    lines = ["Downloaded from OpenSubtitles"] + DIALOGUE
    p = tmp_path / "a.srt"
    write(p, make_srt(lines))

    clean_subtitle(str(p))

    assert [s.index for s in pysrt.open(str(p))] == list(range(1, len(DIALOGUE) + 1))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_file_mode_is_preserved(tmp_path):
    p = tmp_path / "a.srt"
    write(p, make_srt(DIALOGUE), bom=True)
    os.chmod(p, 0o664)

    clean_subtitle(str(p))

    assert stat.S_IMODE(os.stat(p).st_mode) == 0o664
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".talkarr-")]


def test_decode_subtitle():
    assert decode_subtitle("héllo".encode()) == ("héllo", "utf-8")
    text, enc = decode_subtitle("Le café est très bon, n'est-ce pas?".encode("cp1252"))
    assert text == "Le café est très bon, n'est-ce pas?"
    assert enc != "utf-8"


def test_central_european_text_keeps_cp1250():
    text = "Zażółć gęślą jaźń. Dzień dobry, proszę pana."
    decoded, enc = decode_subtitle(text.encode("cp1250"))
    assert decoded == text
    assert enc == "cp1250"
