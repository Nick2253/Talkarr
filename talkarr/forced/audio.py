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
import random
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
    # talkarr-ml scores short regions on >= 3 s of surrounding audio, so only very
    # short blips are too thin to trust on their own.
    min_confident_seconds: float = 0.5
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
SLACK = 0.3  # seconds of tolerance around speech (cue timing vs VAD edges)
REFINE_STEP = 0.02
REFINE_REACH = 1.5  # seconds around the best placed-count range searched by overlap
# Chance baseline: the forced cues' durations at random times in the film, fitted the
# same way. With hundreds of (partly false) foreign spans and few cues, some offset
# always places a few cues by luck; a real fit has to beat what luck achieves.
CHANCE_TRIALS = 5


@dataclass
class TimelineFit:
    scale: float
    offset: float
    placed: int  # forced cues whose midpoint sits on foreign speech after the transform
    placed_identity: int  # ...and before it
    overlap: float = 0.0  # seconds of forced-cue time on foreign speech after...
    overlap_identity: float = 0.0  # ...and before
    chance: int = 0  # most cues the same search places for decoy cues

    def apply(self, t):
        return self.scale * t + self.offset


def fit_timeline(cues, spans, scales=SCALES, max_offset=MAX_OFFSET):
    """Best scale/offset for ``cues`` against foreign ``spans``; None if no spans.

    A cue is *placed* when its midpoint falls on foreign speech (± SLACK). For each
    scale, the offsets that place a cue form a union of intervals, so a sweep over all
    cues' intervals finds exactly the offset range placing the most cues (robust to
    cues with no audio). Within that range, total overlap in seconds picks the precise
    offset, which separates nearby offsets/framerates that place the same cues.
    """
    if not spans or not cues:
        return None
    index = _SpanIndex(spans)
    best = None
    for scale in scales:
        count, lo, hi = _best_range(cues, index, scale, max_offset)
        if count == 0 and scale != 1.0:
            continue
        if count == 0:
            lo = hi = 0.0
        # The placed-count range finds the neighbourhood; overlap (seconds) decides the
        # exact offset and, across scales, the framerate. Counts are inflated by false
        # foreign spans; overlap rewards cues lining up along their whole length.
        offset, overlap = _refine(cues, index, scale, lo - REFINE_REACH, hi + REFINE_REACH)
        key = (round(overlap, 1), scale == 1.0, -abs(offset))
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
        chance=_chance(cues, index, scales, max_offset),
    )


def _best_range(cues, index, scale, max_offset):
    """(cues placed, offset range [lo, hi]) maximising placed cues at this scale.

    Ties prefer the range closest to no change."""
    events = []
    for cue in cues:
        mid = scale * cue.midpoint
        intervals = []
        for s, e in index.near(mid, max_offset + index.longest):
            lo, hi = s - SLACK - mid, e + SLACK - mid
            lo, hi = max(lo, -max_offset), min(hi, max_offset)
            if lo <= hi:
                intervals.append((lo, hi))
        for lo, hi in _merge(intervals, gap=0.0):
            events.append((lo, 0, +1))
            events.append((hi, 1, -1))  # closes after opens at the same offset
    events.sort()
    count, best = 0, (0, 0.0, 0.0)
    for i, (x, _, delta) in enumerate(events):
        count += delta
        if delta > 0:
            nxt = events[i + 1][0] if i + 1 < len(events) else x
            rng = (count, x, nxt)
            if count > best[0] or (count == best[0] and _distance(rng) < _distance(best)):
                best = rng
    return best


def _distance(rng):
    _, lo, hi = rng
    return 0.0 if lo <= 0.0 <= hi else min(abs(lo), abs(hi))


def _chance(cues, index, scales, max_offset, trials=CHANCE_TRIALS):
    """Most cues the search places for decoys: this subtitle's own cue pattern moved
    far from where it is (wrapping around the film), so the baseline keeps its
    clustering (forced cues come in scenes, which match clustered speech more easily
    than scattered cues would). Deterministic."""
    rng = random.Random(len(cues) * 7919 + len(index.spans))
    horizon = max(index.spans[-1][1], *(c.end for c in cues))
    if horizon <= 4 * max_offset:
        return 0
    best = 0
    for _ in range(trials):
        shift = rng.uniform(2 * max_offset, horizon - 2 * max_offset)
        decoys = []
        for c in cues:
            t = (c.start + shift) % horizon
            decoys.append(_Interval(t, t + (c.end - c.start)))
        decoys.sort(key=lambda d: d.start)
        for scale in scales:
            best = max(best, _best_range(decoys, index, scale, max_offset)[0])
    return best


@dataclass(frozen=True)
class _Interval:
    start: float
    end: float

    @property
    def midpoint(self):
        return (self.start + self.end) / 2


def _placed(cues, index, scale, offset):
    return sum(1 for c in cues if index.contains(scale * c.midpoint + offset, SLACK))


def _overlap(cues, index, scale, offset):
    return sum(index.overlap(scale * c.start + offset, scale * c.end + offset) for c in cues)


def _refine(cues, index, scale, lo, hi):
    """Offset in [lo, hi] with the most overlap; centre of the plateau on ties."""
    steps = max(1, int((hi - lo) / REFINE_STEP))
    scored = [
        (
            round(_overlap(cues, index, scale, lo + k * (hi - lo) / steps), 3),
            lo + k * (hi - lo) / steps,
        )
        for k in range(steps + 1)
    ]
    peak = max(v for v, _ in scored)
    plateau = [o for v, o in scored if v >= peak - 1e-6]
    return round((min(plateau) + max(plateau)) / 2, 3), peak


class _SpanIndex:
    def __init__(self, spans):
        self.spans = sorted(spans)
        self.starts = [s for s, _ in self.spans]
        self.longest = max((e - s for s, e in self.spans), default=0.0)

    def contains(self, t, slack=0.0):
        i = bisect.bisect_right(self.starts, t + slack) - 1
        while i >= 0 and self.spans[i][0] >= t - slack - self.longest:
            s, e = self.spans[i]
            if s - slack <= t <= e + slack:
                return True
            i -= 1
        return False

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
