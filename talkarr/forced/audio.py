"""Audio anchors: where non-English speech actually is in the film.

Forced cues belong where foreign dialogue is spoken. The foreign-speech timeline is
as sparse as the forced subtitle itself, so aligning one to the other doesn't suffer
the problem that makes ordinary VAD sync destroy forced subtitles (sparse cues
matched against *all* speech).

Steps (docs/PLAN.md section 3, S2):
1. ``choose_audio_stream`` — main dialogue track, never commentary/description.
2. ``extract_dialogue`` — centre channel if the layout has one, else a downmix;
   16 kHz mono Opus (small enough to send to talkarr-ml).
3. ``analyze`` — talkarr-ml returns VAD speech regions with language scores.
4. ``foreign_timeline`` — regions classified English / foreign / uncertain, with
   short or low-margin regions resolved from their neighbours, never the reverse.
5. ``fit_timeline`` — the scale/offset that puts the most forced cues on foreign
   speech, over the same framerate ratios as the text path.

Models never run here; talkarr-ml (on the AI host) does that.
"""

import bisect
import json
import subprocess
from dataclasses import dataclass, field

import requests

from .fit import SCALES

# --- 1/2: probe and extract ----------------------------------------------------------

EXCLUDED_DISPOSITIONS = ("comment", "visual_impaired", "hearing_impaired", "karaoke")
EXCLUDED_TITLE_WORDS = ("commentary", "description", "descriptive", "director", "isolated")


@dataclass(frozen=True)
class AudioStream:
    index: int  # ffmpeg stream index
    channels: int
    layout: str
    language: str
    title: str

    @property
    def has_center(self):
        return self.layout in CENTER_LAYOUTS


# ffmpeg channel layouts that include a front-centre (FC) channel. Channel count alone
# isn't enough: 2.1, quad and 3.0(back) have three or more channels and no centre.
CENTER_LAYOUTS = frozenset(
    {
        "3.0",
        "3.1",
        "4.0",
        "4.1",
        "5.0",
        "5.0(side)",
        "5.1",
        "5.1(side)",
        "6.0",
        "6.1",
        "6.1(back)",
        "7.0",
        "7.0(front)",
        "7.1",
        "7.1(wide)",
        "7.1(wide-side)",
        "hexagonal",
        "octagonal",
        "5.1.2",
        "5.1.4",
        "7.1.2",
        "7.1.4",
        "7.2.3",
    }
)


def probe_audio_streams(video):
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index,channels,channel_layout:stream_tags=language,title:stream_disposition",
            "-of",
            "json",
            video,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = []
    for s in json.loads(proc.stdout).get("streams", []):
        disposition = s.get("disposition", {})
        tags = s.get("tags", {})
        title = tags.get("title", "")
        if any(disposition.get(d) for d in EXCLUDED_DISPOSITIONS):
            continue
        if any(word in title.lower() for word in EXCLUDED_TITLE_WORDS):
            continue
        streams.append(
            AudioStream(
                s["index"],
                s.get("channels", 2),
                s.get("channel_layout", ""),
                tags.get("language", ""),
                title,
            )
        )
    return streams


def choose_audio_stream(streams, primary_language="eng"):
    """Primary-language track with the most channels; else the first usable one."""
    if not streams:
        return None
    tagged = [s for s in streams if s.language in (primary_language, primary_language[:2])]
    pool = tagged or [s for s in streams if s.language in ("", "und")] or streams
    return max(pool, key=lambda s: s.channels)


def extract_dialogue(video, stream, output_path, timeout=1800):
    """Dialogue audio as 16 kHz mono Opus."""
    mix = "pan=mono|c0=FC" if stream.has_center else "pan=mono|c0=0.5*c0+0.5*c1"
    if stream.channels == 1:
        mix = "anull"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            video,
            "-map",
            f"0:{stream.index}",
            "-vn",
            "-af",
            mix,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            output_path,
        ],
        check=True,
        timeout=timeout,
    )
    return output_path


# --- 3: analyse -----------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    start: float
    end: float
    p_en: float
    top: tuple = ()  # ((lang, prob), ...)

    @property
    def duration(self):
        return self.end - self.start

    @property
    def margin(self):
        """Gap between the top two language scores (low = the model is guessing)."""
        probs = [p for _, p in self.top]
        return probs[0] - probs[1] if len(probs) > 1 else (probs[0] if probs else 0.0)


def analyze(audio_path, url, timeout=1800):
    with open(audio_path, "rb") as f:
        resp = requests.post(f"{url.rstrip('/')}/v1/analyze", files={"audio": f}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def regions_from_response(data):
    return [
        Region(r["start"], r["end"], r["p_en"], tuple(tuple(t) for t in r.get("top", ())))
        for r in data["regions"]
    ]


# --- 4: classify ----------------------------------------------------------------------

ENGLISH, FOREIGN, UNCERTAIN = "en", "foreign", "uncertain"


@dataclass(frozen=True)
class TimelineParams:
    foreign_below: float = 0.2  # p_en under this is foreign...
    english_above: float = 0.6  # ...over this is English; between is uncertain
    min_confident_seconds: float = 1.2  # shorter regions can't be confident alone
    min_margin: float = 0.05  # top-2 gap below this: the model is guessing (noise, music)
    context_seconds: float = 20.0  # neighbours within this gap inform uncertain regions


@dataclass
class Timeline:
    regions: list
    labels: list
    foreign: list = field(default_factory=list)  # [(start, end)] merged foreign spans

    @property
    def foreign_seconds(self):
        return sum(e - s for s, e in self.foreign)


def foreign_timeline(regions, params=None):
    p = params or TimelineParams()
    labels = [_own_label(r, p) for r in regions]
    resolved = list(labels)
    for i, label in enumerate(labels):
        if label != UNCERTAIN:
            continue
        # An uncertain region takes the label of confident neighbours only when
        # both sides that exist (within the context window) agree.
        left = _confident_neighbour(regions, labels, i, -1, p)
        right = _confident_neighbour(regions, labels, i, +1, p)
        votes = {v for v in (left, right) if v is not None}
        if len(votes) == 1:
            resolved[i] = votes.pop()
    spans = _merge(
        [(r.start, r.end) for r, lab in zip(regions, resolved, strict=True) if lab == FOREIGN]
    )
    return Timeline(regions, resolved, spans)


def _own_label(r, p):
    if r.duration < p.min_confident_seconds or (r.top and r.margin < p.min_margin):
        return UNCERTAIN
    if r.p_en <= p.foreign_below:
        return FOREIGN
    if r.p_en >= p.english_above:
        return ENGLISH
    return UNCERTAIN


def _confident_neighbour(regions, labels, i, step, p):
    j = i + step
    while 0 <= j < len(regions):
        gap = regions[i].start - regions[j].end if step < 0 else regions[j].start - regions[i].end
        if gap > p.context_seconds:
            return None
        if labels[j] != UNCERTAIN:
            return labels[j]
        j += step
    return None


def _merge(spans, gap=0.3):
    merged = []
    for s, e in sorted(spans):
        if merged and s - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


# --- 5: fit ---------------------------------------------------------------------------

MAX_OFFSET = 300.0  # seconds searched either way
ON_SPEECH = 0.4  # a cue counts as placed when this share of it overlaps foreign speech
SLACK = 0.3  # seconds of tolerance around speech (cue timing vs VAD edges)


@dataclass
class TimelineFit:
    scale: float
    offset: float
    placed: int  # forced cues on foreign speech after the transform
    placed_identity: int  # ...and before it
    overlap: float = 0.0  # seconds of forced-cue time on foreign speech after...
    overlap_identity: float = 0.0  # ...and before

    def apply(self, t):
        return self.scale * t + self.offset


REFINE_REACH = 1.5  # seconds around a coarse candidate searched for the overlap peak
REFINE_STEP = 0.02
MAX_REFINED = 12  # coarse candidates refined per scale


def fit_timeline(cues, spans, scales=SCALES, max_offset=MAX_OFFSET):
    """Best scale/offset for ``cues`` against foreign ``spans``; None if no spans.

    Coarse: offsets that put a cue start on a span start, ranked by how many cues end
    up on foreign speech (robust to cues with no audio). Fine: around the best coarse
    candidates, maximise total overlap in seconds, which peaks at the true alignment
    and separates nearby offsets/framerates that place the same number of cues.
    """
    if not spans or not cues:
        return None
    index = _SpanIndex(spans)
    best = None
    for scale in scales:
        candidates = {0.0} if scale == 1.0 else set()
        for cue in cues:
            t = scale * cue.start
            for s, _ in index.near(t, max_offset):
                candidates.add(round(s - t, 2))
        if not candidates:
            continue  # no foreign speech within reach of any cue at this framerate
        ranked = sorted(((_placed(cues, index, scale, o), o) for o in candidates), reverse=True)
        top = ranked[0][0]
        for placed, coarse in ranked[:MAX_REFINED]:
            if placed < top:
                break
            offset, overlap = _refine(cues, index, scale, coarse)
            key = (_placed(cues, index, scale, offset), round(overlap, 1), scale == 1.0)
            if best is None or key > best[0]:
                best = (key, scale, offset, overlap)
    _, scale, offset, overlap = best
    return TimelineFit(
        scale,
        offset,
        _placed(cues, index, scale, offset),
        _placed(cues, index, 1.0, 0.0),
        round(overlap, 3),
        round(_overlap(cues, index, 1.0, 0.0), 3),
    )


def _placed(cues, index, scale, offset):
    count = 0
    for cue in cues:
        s, e = scale * cue.start + offset, scale * cue.end + offset
        if e > s and index.overlap(s - SLACK, e + SLACK) >= ON_SPEECH * (e - s):
            count += 1
    return count


def _overlap(cues, index, scale, offset):
    return sum(index.overlap(scale * c.start + offset, scale * c.end + offset) for c in cues)


def _refine(cues, index, scale, offset):
    """Offset near ``offset`` with the most overlap; centre of the plateau on ties."""
    steps = int(REFINE_REACH / REFINE_STEP)
    scored = [
        (round(_overlap(cues, index, scale, offset + k * REFINE_STEP), 3), offset + k * REFINE_STEP)
        for k in range(-steps, steps + 1)
    ]
    peak = max(v for v, _ in scored)
    plateau = [o for v, o in scored if v >= peak - 1e-6]
    return round((min(plateau) + max(plateau)) / 2, 3), peak


class _SpanIndex:
    def __init__(self, spans):
        self.spans = sorted(spans)
        self.starts = [s for s, _ in self.spans]

    def near(self, t, reach):
        lo = bisect.bisect_left(self.starts, t - reach)
        hi = bisect.bisect_right(self.starts, t + reach)
        return self.spans[lo:hi]

    def overlap(self, a, b):
        total = 0.0
        i = max(0, bisect.bisect_right(self.starts, a) - 1)
        while i < len(self.spans) and self.spans[i][0] < b:
            s, e = self.spans[i]
            total += max(0.0, min(b, e) - max(a, s))
            i += 1
        return total
