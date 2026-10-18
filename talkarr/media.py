"""Locating the video a sidecar subtitle belongs to."""

import os

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts")


def find_video_for_subtitle(subtitle_path):
    """Find the video a sidecar subtitle belongs to (see ``video_name_for``)."""
    directory = os.path.dirname(subtitle_path)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    name = video_name_for(os.path.basename(subtitle_path), names)
    return os.path.join(directory, name) if name else None


def video_name_for(subtitle_name, names):
    """The video file in ``names`` (one folder's listing) a subtitle belongs to.

    ``Movie.Name.2020.en.forced.srt`` belongs to ``Movie.Name.2020.<ext>``: strip
    trailing dot-separated parts one at a time until a video with that stem exists.
    Only if no stem matches and the folder holds exactly one video is that video
    used; guessing in a season folder would pair one episode's subtitles with
    another episode. Pure: one listing serves every subtitle in the folder.
    """
    videos = [n for n in names if n.lower().endswith(VIDEO_EXTENSIONS)]
    by_stem = {os.path.splitext(v)[0]: v for v in videos}
    stem = os.path.splitext(subtitle_name)[0]
    while stem:
        if stem in by_stem:
            return by_stem[stem]
        if "." not in stem:
            break
        stem = stem.rsplit(".", 1)[0]
    return videos[0] if len(videos) == 1 else None


SUB_EXT = ".srt"
FORCED_TAGS = (".forced", ".foreign")
ENGLISH_TAGS = (".en", ".eng", ".english")
# Preference order for the full English reference subtitle.
REFERENCE_SUFFIXES = (
    ".en.srt",
    ".eng.srt",
    ".english.srt",
    ".en.sdh.srt",
    ".en.hi.srt",
    ".en.cc.srt",
    ".eng.sdh.srt",
    ".eng.hi.srt",
)


def english_forced_stem(name):
    """``Movie.en.forced.srt`` -> ``Movie``; ``None`` if not an English forced sub."""
    lower = name.lower()
    if not lower.endswith(SUB_EXT):
        return None
    base = lower[: -len(SUB_EXT)]
    for forced in FORCED_TAGS:
        if base.endswith(forced):
            rest = base[: -len(forced)]
            for lang in ENGLISH_TAGS:
                if rest.endswith(lang):
                    return name[: len(rest) - len(lang)]
    return None


def find_reference_subtitle(forced_path):
    """The full English subtitle next to an English forced subtitle, or ``None``."""
    directory, name = os.path.split(forced_path)
    stem = english_forced_stem(name)
    if stem is None:
        return None
    names = set(os.listdir(directory))
    for suffix in REFERENCE_SUFFIXES:
        if stem + suffix in names:
            return os.path.join(directory, stem + suffix)
    return None
