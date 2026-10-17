import json
from types import SimpleNamespace

import pytest
import responses

from talkarr.forced import audio
from talkarr.forced.audio import (
    ENGLISH,
    FOREIGN,
    UNCERTAIN,
    AudioStream,
    Region,
    choose_audio_stream,
    fit_timeline,
    foreign_timeline,
)
from talkarr.forced.subtitles import Cue


def region(start, end, p_en, top=(("ru", 0.7), ("bg", 0.1))):
    return Region(start, end, p_en, top)


# --- streams ------------------------------------------------------------------------


def stream(index, channels=6, layout="5.1(side)", language="eng", title=""):
    return AudioStream(index, channels, layout, language, title)


@pytest.mark.parametrize(
    ("layout", "center"),
    [
        ("5.1(side)", True),
        ("7.1", True),
        ("3.0", True),
        ("stereo", False),
        ("2.1", False),
        ("quad", False),
        ("3.0(back)", False),
        ("mono", False),
    ],
)
def test_has_center(layout, center):
    assert stream(0, layout=layout).has_center is center


def test_choose_prefers_primary_language_then_channels():
    streams = [
        stream(1, 2, "stereo", "eng"),
        stream(2, 6, "5.1", "fre"),
        stream(3, 8, "7.1", "eng"),
    ]
    assert choose_audio_stream(streams).index == 3


def test_choose_falls_back_to_untagged_then_any():
    assert (
        choose_audio_stream([stream(1, language="fre"), stream(2, 2, "stereo", "und")]).index == 2
    )
    assert choose_audio_stream([stream(4, language="fre")]).index == 4
    assert choose_audio_stream([]) is None


def test_probe_skips_commentary_and_description(monkeypatch):
    ffprobe = {
        "streams": [
            {
                "index": 1,
                "channels": 6,
                "channel_layout": "5.1",
                "tags": {"language": "eng"},
                "disposition": {"default": 1},
            },
            {
                "index": 2,
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng", "title": "Director's Commentary"},
                "disposition": {},
            },
            {
                "index": 3,
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng"},
                "disposition": {"visual_impaired": 1},
            },
            {
                "index": 4,
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng", "title": "Audio Description"},
                "disposition": {},
            },
        ]
    }
    monkeypatch.setattr(
        audio.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(ffprobe))
    )
    assert [s.index for s in audio.probe_audio_streams("x.mkv")] == [1]


@pytest.mark.parametrize(
    ("st", "expected"),
    [
        (stream(1), "pan=mono|c0=FC"),
        (stream(1, 2, "stereo"), "pan=mono|c0=0.5*c0+0.5*c1"),
        (stream(1, 1, "mono"), "anull"),
    ],
)
def test_extract_dialogue_mix(monkeypatch, st, expected):
    calls = []
    monkeypatch.setattr(audio.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    audio.extract_dialogue("in.mkv", st, "out.opus")
    cmd = calls[0]
    assert cmd[cmd.index("-af") + 1] == expected
    assert cmd[cmd.index("-map") + 1] == "0:1"


@responses.activate
def test_analyze_posts_audio(tmp_path):
    f = tmp_path / "a.opus"
    f.write_bytes(b"opus")
    responses.post(
        "http://ml/v1/analyze",
        json={
            "duration": 1.0,
            "regions": [{"start": 1.0, "end": 2.5, "p_en": 0.1, "top": [["ru", 0.8], ["uk", 0.1]]}],
        },
    )
    data = audio.analyze(str(f), "http://ml/")
    regions = audio.regions_from_response(data)
    assert regions == [Region(1.0, 2.5, 0.1, (("ru", 0.8), ("uk", 0.1)))]
    assert regions[0].margin == pytest.approx(0.7)


# --- timeline -----------------------------------------------------------------------


def test_confident_regions_keep_their_labels():
    tl = foreign_timeline([region(0, 3, 0.05), region(10, 14, 0.9, (("en", 0.9), ("sco", 0.05)))])
    assert tl.labels == [FOREIGN, ENGLISH]
    assert tl.foreign == [(0, 3)]


def test_short_region_inside_foreign_scene_becomes_foreign():
    regions = [region(0, 3, 0.05), region(4, 4.3, 0.7), region(6, 9, 0.02)]
    tl = foreign_timeline(regions)
    assert tl.labels == [FOREIGN, FOREIGN, FOREIGN]
    assert tl.foreign == [(0, 3), (4, 4.3), (6, 9)]


def test_short_region_with_confident_scores_keeps_its_label():
    """talkarr-ml scores short regions with surrounding context, so a 0.8 s line with
    p_en 0.00 and a clear margin is foreign on its own."""
    tl = foreign_timeline([region(137.6, 138.4, 0.0, (("fa", 0.92), ("ar", 0.03)))])
    assert tl.labels == [FOREIGN]


def test_isolated_confident_foreign_line_survives_english_scene():
    en = (("en", 0.95), ("sco", 0.02))
    regions = [region(0, 5, 0.95, en), region(6, 8, 0.03), region(9, 14, 0.97, en)]
    assert foreign_timeline(regions).labels == [ENGLISH, FOREIGN, ENGLISH]


def test_noise_with_no_margin_is_not_foreign():
    noise = region(5, 7, 0.01, (("sr", 0.11), ("bs", 0.10)))
    tl = foreign_timeline([noise])
    assert tl.labels == [UNCERTAIN] and tl.foreign == []


def test_uncertain_between_disagreeing_neighbours_stays_uncertain():
    en = (("en", 0.95), ("sco", 0.02))
    regions = [region(0, 3, 0.03), region(4, 4.5, 0.4), region(5, 9, 0.95, en)]
    assert foreign_timeline(regions).labels[1] == UNCERTAIN


def test_context_does_not_reach_across_long_gaps():
    regions = [region(0, 3, 0.03), region(100, 100.8, 0.4)]
    assert foreign_timeline(regions).labels == [FOREIGN, UNCERTAIN]


# --- fit ----------------------------------------------------------------------------


def film_spans():
    return [(100.0 + 37 * i + (i % 3), 102.5 + 37 * i + (i % 3)) for i in range(25)]


def cues_on(spans):
    return [Cue(s + 0.1, e - 0.1, "line") for s, e in spans]


def test_fit_identity_when_in_sync():
    spans = film_spans()
    fit = fit_timeline(cues_on(spans), spans)
    assert fit.scale == 1.0 and abs(fit.offset) < 0.3
    assert fit.placed == fit.placed_identity == 25


@pytest.mark.parametrize(("scale", "offset"), [(1.0, 7.0), (1.0, -42.0), (23.976 / 25, 0.0)])
def test_fit_recovers_shift_and_framerate(scale, offset):
    spans = film_spans()
    good = cues_on(spans)
    broken = [Cue((c.start - offset) / scale, (c.end - offset) / scale, c.text) for c in good]
    fit = fit_timeline(broken, spans)
    assert fit.scale == pytest.approx(scale)
    assert fit.offset == pytest.approx(offset, abs=0.3)
    assert fit.placed == 25
    assert fit.placed_identity < 5


def test_visual_cues_do_not_break_the_fit():
    """Baby Driver: some forced cues have no foreign audio at all."""
    spans = film_spans()
    cues = cues_on(spans) + [Cue(2000.0 + 50 * i, 2003.0 + 50 * i, "signed") for i in range(6)]
    shifted = [Cue(c.start + 5, c.end + 5, c.text) for c in cues]
    fit = fit_timeline(shifted, spans)
    assert fit.offset == pytest.approx(-5, abs=0.3)
    assert fit.placed == 25


def test_fit_without_spans():
    assert fit_timeline([Cue(1, 2, "x")], []) is None


# --- verdicts -----------------------------------------------------------------------

from talkarr.forced.verify import Verdict, verify_audio  # noqa: E402


def judge(cues, spans):
    return verify_audio(cues, fit_timeline(cues, spans), spans)


def test_audio_verdict_in_sync():
    spans = film_spans()
    assert judge(cues_on(spans), spans).verdict == Verdict.IN_SYNC


def test_audio_verdict_retimed():
    spans = film_spans()
    shifted = [Cue(c.start + 9, c.end + 9, c.text) for c in cues_on(spans)]
    report = judge(shifted, spans)
    assert report.verdict == Verdict.RETIMED
    assert report.offset == pytest.approx(-9, abs=0.3)
    assert not report.review


def test_audio_verdict_unrelated_cues_are_unverifiable():
    """No placement beats chance: the audio path can't tell a wrong subtitle from a
    mostly-visual one, so it says so instead of claiming either."""
    spans = film_spans()
    unrelated = [Cue(3000 + 61.7 * i, 3001.5 + 61.7 * i, "x") for i in range(20)]
    report = judge(unrelated, spans)
    assert report.verdict == Verdict.UNVERIFIABLE
    assert "beats chance" in report.reason


def test_audio_verdict_unverifiable_without_foreign_speech():
    """Baby Driver-style: forced cues for sign language, no foreign audio at all."""
    cues = [Cue(1000 + 5 * i, 1003 + 5 * i, "signed") for i in range(10)]
    report = judge(cues, [])
    assert report.verdict == Verdict.UNVERIFIABLE
    assert "too little foreign speech" in report.reason


def test_audio_verdict_mislabelled_and_empty():
    spans = film_spans()
    assert judge([Cue(i, i + 1, "x") for i in range(300)], spans).verdict == Verdict.MISLABELLED
    assert verify_audio([], None, spans).verdict == Verdict.UNVERIFIABLE


def test_fit_when_speech_is_out_of_reach_at_some_framerates():
    spans = film_spans()  # 100 s .. 1000 s
    far = [Cue(5000 + 10 * i, 5002 + 10 * i, "x") for i in range(5)]
    fit = fit_timeline(far, spans, max_offset=60)
    assert fit.scale == 1.0 and fit.offset == pytest.approx(0, abs=1.6)
    assert fit.placed == 0


def test_few_cues_on_noisy_timeline_never_retime():
    """MI3 / Live Free or Die Hard: 6 cues against hundreds of partly false foreign
    spans. Some offset always places most cues by luck; that must not become a retime."""
    import random

    rng = random.Random(1)
    noisy = sorted((t, t + rng.uniform(0.5, 3)) for t in (rng.uniform(0, 7000) for _ in range(400)))
    cues = [Cue(1000 + 400 * i, 1002 + 400 * i, "x") for i in range(6)]
    report = judge(cues, audio._merge(noisy))
    assert report.verdict == Verdict.UNVERIFIABLE
    assert report.details["chance"] >= 4
