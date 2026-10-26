import random

import pytest

from talkarr.forced import subtitles as S
from talkarr.forced.anchors import has_foreign_annotations, text_anchors
from talkarr.forced.fit import fit_anchors
from talkarr.forced.subtitles import Cue
from talkarr.forced.verify import Verdict, verify

WORDS = [
    "captain",
    "submarine",
    "orders",
    "cabin",
    "political",
    "officer",
    "crew",
    "engine",
    "reactor",
    "torpedo",
    "course",
    "depth",
    "silence",
    "harbor",
    "admiral",
    "signal",
    "radio",
    "contact",
    "sonar",
    "periscope",
    "chart",
    "morning",
    "evening",
    "cold",
    "warm",
    "quiet",
    "storm",
    "river",
    "bridge",
    "market",
    "station",
    "letter",
]


def sentence(rng):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 8))).capitalize() + "."


@pytest.fixture
def film():
    """A 300-cue full subtitle and a 25-cue forced subtitle drawn from it."""
    rng = random.Random(7)
    full, t = [], 30.0
    for _ in range(300):
        dur = rng.uniform(1.5, 4.0)
        full.append(Cue(t, t + dur, sentence(rng)))
        t += dur + rng.uniform(0.5, 20.0)
    picks = sorted(rng.sample(range(300), 25))
    forced = [full[i] for i in picks]
    return full, forced


def starts(cues):
    return [c.start for c in cues]


# --- subtitles -------------------------------------------------------------------


def test_parse_compose_roundtrip():
    cues = [Cue(1.0, 2.5, "Hello"), Cue(3600.25, 3601.0, "Two\nlines")]
    assert S.parse(S.compose(cues)) == cues


def test_load_handles_bom_and_cp1252(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes(b"\xef\xbb\xbf" + S.compose([Cue(1, 2, "Hi")]).encode())
    assert S.load(str(p)) == [Cue(1.0, 2.0, "Hi")]
    p.write_bytes(S.compose([Cue(1, 2, "Très bien, n'est-ce pas?")]).encode("cp1252"))
    assert S.load(str(p))[0].text == "Très bien, n'est-ce pas?"


def test_transform_and_clamp():
    out = S.transform([Cue(1.0, 2.0, "x")], scale=2.0, offset=-3.0)
    assert out == [Cue(0.0, 1.0, "x")]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("It’s cold this morning, Captain.", "it's cold this morning captain"),
        ("<i>“Armageddon.”</i>", "Armageddon"),
        ("- RAMIUS: Very well.\n- Yes.", "very well yes"),
        ("[DOOR SLAMS] Café!", "cafe"),
    ],
)
def test_normalize_equivalences(a, b):
    assert S.normalize(a) == S.normalize(b)


# --- anchors + fit ----------------------------------------------------------------


def test_anchors_tolerate_rewording_and_splits():
    reference = [
        Cue(10, 12, "Cold this morning, Captain."),
        Cue(50, 51, "The political officer"),
        Cue(51.2, 53, "is in your cabin."),
    ]
    forced = [
        Cue(10, 12, "It's cold this morning, Captain."),
        Cue(50, 53, "The political officer is in your cabin."),
    ]
    anchors = text_anchors(forced, reference)
    assert {(a.forced_index, a.reference_time) for a in anchors} >= {(0, 10), (1, 50)}


def test_generic_short_lines_are_not_anchors():
    anchors = text_anchors([Cue(1, 2, "No.")], [Cue(1, 2, "No.")])
    assert anchors == []


def test_fit_recovers_offset_despite_repeated_lines(film):
    full, forced = film
    # A repeated generic line in the reference creates decoy candidates.
    decoys = full + [Cue(5000 + i * 100, 5001 + i * 100, forced[0].text) for i in range(5)]
    shifted = S.transform(forced, offset=7.5)
    fit = fit_anchors(text_anchors(shifted, decoys))
    assert fit.scale == 1.0
    assert fit.offset == pytest.approx(-7.5, abs=0.01)
    assert fit.inlier_count == len(forced)


def test_fit_none_without_anchors():
    assert fit_anchors([]) is None


def test_foreign_annotations():
    cues = [Cue(1, 2, "[speaking Russian]"), Cue(3, 4, "(in German)"), Cue(5, 6, "Hello")]
    assert has_foreign_annotations(cues)
    assert not has_foreign_annotations(cues[2:])


# --- verify -----------------------------------------------------------------------


def test_good_forced_sub_is_in_sync(film):
    full, forced = film
    report = verify(forced, full)
    assert report.verdict == Verdict.IN_SYNC
    assert report.inliers == len(forced)


@pytest.mark.parametrize(
    ("scale", "offset"),
    [
        (1.0, 0.5),
        (1.0, -2.0),
        (1.0, 60.0),
        (23.976 / 25, 0.0),
        (25 / 23.976, 3.0),
        (24 / 23.976, 0),
    ],
)
def test_broken_timing_is_retimed_back(film, scale, offset):
    full, forced = film
    broken = S.transform(forced, scale=scale, offset=offset)

    report = verify(broken, full)

    assert report.verdict == Verdict.RETIMED
    repaired = S.transform(broken, scale=report.scale, offset=report.offset)
    errors = [abs(a - b) for a, b in zip(starts(repaired), starts(forced), strict=True)]
    assert max(errors) < 0.02


def test_cues_without_text_anchor_move_with_the_fit(film):
    """Baby Driver case: forced cues for sign language / on-screen text aren't in
    the full subtitle; they must be re-timed with the rest, not judged wrong."""
    full, forced = film
    visual = [Cue(c.start + 1.0, c.end + 1.0, "SIGNED: unrelated words here") for c in forced[:5]]
    with_visual = sorted(forced + visual, key=lambda c: c.start)
    broken = S.transform(with_visual, offset=4.0)

    report = verify(broken, full)

    assert report.verdict == Verdict.RETIMED
    assert report.offset == pytest.approx(-4.0, abs=0.01)


def test_lines_missing_from_reference_are_unverifiable_not_wrong(film):
    """Another film's forced sub and a full sub that doesn't translate foreign
    dialogue look the same to the text path; it must not claim either is wrong."""
    full, _ = film
    rng = random.Random(99)
    other = [Cue(100 + 60 * i, 102 + 60 * i, f"Zebra {sentence(rng)} quartz") for i in range(20)]
    report = verify(other, full)
    assert report.verdict == Verdict.UNVERIFIABLE
    assert "may not translate" in report.reason


def test_conflicting_timings_are_a_mismatch(film):
    full, forced = film
    # Each line found in the reference, but at unrelated times: no consistent fit.
    rng = random.Random(5)
    scrambled = [Cue(c.start + rng.uniform(-900, 900), c.end, c.text) for c in forced]
    scrambled = [Cue(max(0, c.start), max(0, c.start) + 2, c.text) for c in scrambled]
    assert verify(scrambled, full).verdict == Verdict.MISMATCH


def test_section_of_full_subtitle_is_mislabelled(film):
    full, _ = film
    section = full[100:180]  # 80 consecutive dialogue lines
    report = verify(section, full)
    assert report.verdict == Verdict.MISLABELLED
    assert "section" in report.reason
    # A long track the audio already cleared (a mostly foreign film's) is kept.
    assert verify(section, full, long_ok=True).verdict == Verdict.IN_SYNC


def test_large_offsets_and_framerate_changes_need_review(film):
    full, forced = film
    few = forced[:12]  # too few lines for the fit alone to settle it
    assert not verify(S.transform(few, offset=5), full).review
    assert verify(S.transform(few, offset=-120), full).review
    assert verify(S.transform(few, scale=23.976 / 25), full).review


def test_a_strong_text_fit_needs_no_review_even_when_large(film):
    """The Dictator: 23/23 lines within 1 ms, 428 s out."""
    full, forced = film  # 25 lines
    for moved in (S.transform(forced, offset=-120), S.transform(forced, scale=23.976 / 25)):
        report = verify(moved, full)
        assert report.verdict == Verdict.RETIMED and not report.review


def test_full_subtitle_labelled_forced(film):
    full, _ = film
    report = verify(full, full)
    assert report.verdict == Verdict.MISLABELLED


def test_short_full_subtitle_copy_is_mislabelled():
    rng = random.Random(3)
    full = [Cue(10 * i, 10 * i + 2, sentence(rng)) for i in range(40)]
    assert verify(full[:30], full).verdict == Verdict.MISLABELLED


def test_many_cues_without_reference_is_mislabelled():
    cues = [Cue(i, i + 0.5, "x") for i in range(250)]
    assert verify(cues).verdict == Verdict.MISLABELLED


def test_no_reference_is_unverifiable(film):
    _, forced = film
    report = verify(forced)
    assert report.verdict == Verdict.UNVERIFIABLE


def test_reference_that_only_describes_foreign_speech_is_unverifiable(film):
    full, forced = film
    described = [c for c in full if c not in forced] + [
        Cue(c.start, c.end, "[speaking Russian]") for c in forced
    ]
    report = verify(forced, sorted(described, key=lambda c: c.start))
    assert report.verdict == Verdict.UNVERIFIABLE
    assert "describes foreign speech" in report.reason


def test_empty_forced_sub():
    assert verify([], []).verdict == Verdict.UNVERIFIABLE


def test_report_serialises(film):
    full, forced = film
    d = verify(forced, full).as_dict()
    assert d["verdict"] == "in_sync"
