"""Locating the video a sidecar subtitle belongs to."""

import os

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts")


def find_video_for_subtitle(subtitle_path):
    """Find the video a sidecar subtitle belongs to.

    ``Movie.Name.2020.en.forced.srt`` belongs to ``Movie.Name.2020.<ext>``:
    strip trailing dot-separated parts one at a time until a video with that
    stem exists. Only if no stem matches and the folder holds exactly one
    video is that video used; guessing in a season folder would sync one
    episode's subtitles against another episode.
    """
    directory = os.path.dirname(subtitle_path)
    stem = os.path.splitext(os.path.basename(subtitle_path))[0]

    while stem:
        for ext in VIDEO_EXTENSIONS:
            candidate = os.path.join(directory, stem + ext)
            if os.path.isfile(candidate):
                return candidate
        if "." not in stem:
            break
        stem = stem.rsplit(".", 1)[0]

    videos = [f for f in os.listdir(directory) if f.lower().endswith(VIDEO_EXTENSIONS)]
    if len(videos) == 1:
        return os.path.join(directory, videos[0])

    return None
