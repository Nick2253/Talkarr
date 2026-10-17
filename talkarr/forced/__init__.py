"""Forced-subtitle verification and sync.

Talkarr never generates subtitles. This package checks that an existing forced
subtitle belongs to the video it sits next to and re-times it when it doesn't
line up. Anchors (timed evidence of what is said when) come from a full English
subtitle (the text path) or, later, from translated foreign speech in the audio.
See docs/PLAN.md section 3.
"""
