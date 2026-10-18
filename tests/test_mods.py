import json

import pytest

from talkarr import db, fixes, mods, review, settings, worker
from talkarr.config import Config
from talkarr.db import SubType
from talkarr.forced import subtitles as S
from tests.conftest import make_srt

EN = {"language": "en"}


@pytest.mark.parametrize(
    ("mod", "before", "after"),
    [
        ("remove_tags", "<i>Hello</i> {\\an8}there", "Hello there"),
        ("remove_tags", '<font color="#fff">x</font>', "x"),
        ("emoji", "Hi 😀 there ♪", "Hi there ♪"),
        ("OCR_fixes", "l'm here", "I'm here"),
        ("OCR_fixes", "lt's fine, l know", "It's fine, I know"),
        ("OCR_fixes", "|t's me", "It's me"),
        ("OCR_fixes", "lmpossible!", "Impossible!"),
        ("OCR_fixes", "g0od", "good"),
        ("OCR_fixes", "5 lbs of llama", "5 lbs of llama"),
        ("remove_HI", "[DOOR SLAMS]", ""),
        ("remove_HI", "- JOHN: Hi there.\n- [laughs] Hey!", "- Hi there.\n- Hey!"),
        ("remove_HI", "♪ Fly me to the moon ♪", ""),
        ("remove_HI", "(sighs)\n- Okay then.", "Okay then."),
        ("remove_HI", "GASPS Oh my god.", "Oh my god."),
        ("remove_HI", "MAN: Get down!", "Get down!"),
        ("remove_HI", "Sarah: Wait for me.", "Wait for me."),
        ("remove_HI", "Note: this is it", "Note: this is it"),
        ("remove_HI", "The time is 10:30.", "The time is 10:30."),
        ("common", "Hello ,world", "Hello, world"),
        ("common", "...and then", "and then"),
        ("common", "i think i'm right", "I think I'm right"),
        ("common", "Wait -- what?", "Wait — what?"),
        ("common", "heIIo there", "hello there"),
        ("common", "McIntosh is here", "McIntosh is here"),
        ("common", "the end. and more", "the end. And more"),
        ("common", "Mr. smith arrived", "Mr. smith arrived"),
        ("common", "What?.", "What?"),
        ("common", "Really?!", "Really?!"),
        ("common", "visit www.example.com now", "visit www.example.com now"),
        ("common", "''Quote'' don’t", '"Quote" don\'t'),
        ("common", "-Yes", "- Yes"),
        ("common", "Hello  there....", "Hello there..."),
        ("fix_uppercase", "I DON'T KNOW. WHAT?", "I don't know. What?"),
        ("fix_uppercase", "- WELL-KNOWN MAN.\n- i'm OK.", "- Well-known man.\n- I'm ok."),
        ("color", "Hi", '<font color="#FFFF00">Hi</font>'),
        ("color", '<font color="#FF0000">Hi</font>', '<font color="#FFFF00">Hi</font>'),
    ],
)
def test_modification(mod, before, after):
    args = {"name": "yellow"} if mod == "color" else {}
    assert mods._tidy(mods._FUNCS[mod](before, **EN, **args)) == after


@pytest.mark.parametrize(
    ("before", "after"), [(".שלום", "שלום."), ("?מה -", "- מה?"), ("שלום.", "שלום.")]
)
def test_reverse_rtl_only_flips_broken_lines(before, after):
    assert mods._reverse_rtl(before) == after


def test_parse_uses_bazarr_names_in_run_order():
    chosen, unknown = mods.parse("common, Remove_HI,color(name=yellow), bogus, color(name=puce)")
    assert chosen == [("remove_HI", {}), ("common", {}), ("color", {"name": "yellow"})]
    assert unknown == ["bogus", "color(name=puce)"]
    assert mods.parse("") == ([], [])


def write(tmp_path, texts, name="Film.en.srt"):
    p = tmp_path / name
    p.write_text(make_srt(texts))
    return str(p)


DIALOGUE = [f"Line {i} of the dialogue." for i in range(20)]


def test_run_reports_without_writing_then_writes(tmp_path):
    path = write(tmp_path, ["<i>Hello</i>", "[DOOR SLAMS]", *DIALOGUE])
    before = open(path).read()
    chosen = mods.parse("remove_tags,remove_HI")[0]

    trial = mods.run(path, chosen)
    assert trial.changes == {"remove_tags": 1, "remove_HI": 1}
    assert open(path).read() == before

    applied = mods.run(path, chosen, write=True)
    assert applied.cues_after == 21
    cues = S.load(path)
    assert cues[0].text == "Hello" and cues[1].text == DIALOGUE[0]
    assert cues[1].start == pytest.approx(16.0)  # times untouched (it was the third cue)

    assert not mods.run(path, chosen).changed  # a later check proposes nothing


def test_hi_subtitles_keep_their_hi_text(tmp_path):
    path = write(tmp_path, ["[DOOR SLAMS]", *DIALOGUE], name="Film.en.hi.srt")
    assert not mods.run(path, mods.parse("remove_HI")[0], SubType.HI).changed


def test_language_specific_mods_need_their_language(tmp_path):
    path = write(tmp_path, [".שלום", "l'm here", *DIALOGUE], name="Film.he.srt")
    result = mods.run(path, mods.parse("reverse_rtl,OCR_fixes")[0])
    assert result.changes == {"reverse_rtl": 1}


def test_uppercase_fix_only_for_mostly_uppercase_files(tmp_path):
    shouting = [f"THIS IS LINE NUMBER {i} OF IT." for i in range(20)]
    path = write(tmp_path, shouting)
    assert mods.run(path, mods.parse("fix_uppercase")[0]).changes == {"fix_uppercase": 20}
    path = write(tmp_path, ["NASA called.", *DIALOGUE], name="Other.en.srt")
    assert not mods.run(path, mods.parse("fix_uppercase")[0]).changed


def test_refuses_to_empty_a_subtitle(tmp_path):
    path = write(tmp_path, ["[music]"] * 8 + DIALOGUE[:4])
    result = mods.run(path, mods.parse("remove_HI")[0], write=True)
    assert not result.success and "refusing to remove 8 of 12" in result.error
    assert len(S.load(path)) == 12


def test_proposal_splits_safe_and_confident(tmp_path):
    path = write(tmp_path, ["<i>Hi</i>", "l'm here", *DIALOGUE])
    result = mods.run(path, mods.parse("remove_tags,OCR_fixes,color(name=white)")[0])
    built = fixes.build(path, SubType.REGULAR, mods=result)
    assert [(f["summary"], f["tier"]) for f in built] == [
        ("Modify: remove tags (1 cue), colour (22 cues)", "safe"),
        ("Modify: OCR fixes (1 cue)", "confident"),
    ]
    assert built[0]["params"]["mods"] == [["remove_tags", {}], ["color", {"name": "white"}]]


def test_safe_auto_applies_safe_mods_and_the_rest_can_be_applied_later(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "SUBTITLE_MODS", "remove_tags,OCR_fixes")
    monkeypatch.setattr(Config, "SYNC_ENABLED", False)
    settings.set_mode("safe_auto")
    path = write(tmp_path, ["<i>Hi</i>", "l'm here", *DIALOGUE])
    db.create_job("j", path, "", "en")

    worker.process_pending_jobs()

    proposal = json.loads(db.get_job("j")["proposal"])
    assert [(f["kind"], f["tier"], f["state"]) for f in proposal["fixes"]] == [
        ("mods", "safe", "applied"),
        ("mods", "confident", "proposed"),
    ]
    assert S.load(path)[0].text == "Hi"

    review.apply_job("j")  # not refused as "changed since it was checked"
    assert S.load(path)[1].text == "I'm here"
    assert db.get_job("j")["outcome"] == "applied"


def test_settings_lists_unknown_mods(monkeypatch):
    monkeypatch.setattr(Config, "SUBTITLE_MODS", "common,shiny")
    entry = next(s for s in settings.configuration() if s.env == "SUBTITLE_MODS")
    assert entry.value == "common,shiny" and "Not recognised: shiny." in entry.note
