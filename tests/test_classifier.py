import pytest

from talkarr.classifier import classify
from talkarr.db import SubType


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Movie (2020).en.srt", SubType.REGULAR),
        ("Movie (2020).en.forced.srt", SubType.FORCED),
        ("Movie (2020).en.FORCED.srt", SubType.FORCED),
        ("Movie (2020).en.foreign.srt", SubType.FORCED),
        ("Movie (2020).en.signs.srt", SubType.FORCED),
        ("Movie (2020).en.hi.srt", SubType.HI),
        ("Movie (2020).en.sdh.srt", SubType.HI),
        ("Movie (2020).en.cc.srt", SubType.HI),
        ("Show - S01E01.en.hearing-impaired.srt", SubType.HI),
        # Words that merely contain the tags are not tags.
        ("Forced Entry (1975).en.srt", SubType.REGULAR),
        ("Hi-Fi Rush.en.srt", SubType.REGULAR),
    ],
)
def test_filename_classification(name, expected):
    assert classify(f"/media/movies/{name}") == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("en:forced", SubType.FORCED),
        ("en:hi", SubType.HI),
        ("en:sdh", SubType.HI),
        ("EN:FORCED", SubType.FORCED),
    ],
)
def test_language_tag_wins(tag, expected):
    assert classify("/media/Movie.en.srt", tag) == expected


def test_plain_language_tag_falls_back_to_filename():
    assert classify("/media/Movie.en.forced.srt", "en") == SubType.FORCED
