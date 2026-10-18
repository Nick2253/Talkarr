"""Text modifications: the same set Bazarr offers under "Subtitle modifications".

Enabled with ``SUBTITLE_MODS``, using Bazarr's names (its ``subzero_mods`` setting), e.g.
``SUBTITLE_MODS=common,remove_tags,OCR_fixes,color(name=yellow)``:

- ``remove_tags``: drop styling tags (``<i>``, ``<font>``, ``{\\an8}``).
- ``emoji``: drop emoji.
- ``OCR_fixes``: fix characters misread when a subtitle was OCR'd from images (English).
- ``remove_HI``: drop sound descriptions, speaker labels and lyrics. Never applied to
  subtitles named as hearing-impaired: that text is what they are for.
- ``common``: whitespace, punctuation, quote, dash and ellipsis fixes.
- ``fix_uppercase``: give all-uppercase subtitles normal sentence case.
- ``reverse_rtl``: move punctuation to the right end for Hebrew, Arabic and Farsi.
- ``color(name=...)``: colour every cue (one of ``COLORS``).

The rules are Talkarr's own, modelled on what Bazarr's do. Framerate and offset
changes, Bazarr's other modifications, are Talkarr's sync and retime fixes.
"""

import os
import re
from dataclasses import dataclass, field, replace

import emoji as emoji_lib
import ftfy

from .cleaner import UTF8_BOM, decode_subtitle
from .db import SubType
from .fileio import atomic_write_text
from .forced import subtitles as S

SAFE, CONFIDENT = "safe", "confident"


@dataclass(frozen=True)
class Mod:
    id: str
    label: str  # past-tense-free, used as "<label> (N cues)"
    tier: str


# In the order they run (Bazarr's order: OCR before HI before common fixes, case last).
MODS = {
    m.id: m
    for m in (
        Mod("remove_tags", "remove tags", SAFE),
        Mod("emoji", "remove emoji", SAFE),
        Mod("OCR_fixes", "OCR fixes", CONFIDENT),
        Mod("remove_HI", "remove hearing-impaired text", CONFIDENT),
        Mod("common", "common fixes", SAFE),
        Mod("fix_uppercase", "fix uppercase", CONFIDENT),
        Mod("reverse_rtl", "reverse RTL punctuation", SAFE),
        Mod("color", "colour", SAFE),
    )
}

COLORS = {
    "white": "#FFFFFF",
    "light-grey": "#C0C0C0",
    "red": "#FF0000",
    "green": "#00FF00",
    "yellow": "#FFFF00",
    "blue": "#0000FF",
    "magenta": "#FF00FF",
    "cyan": "#00FFFF",
    "black": "#000000",
    "dark-red": "#800000",
    "dark-green": "#008000",
    "dark-yellow": "#808000",
    "dark-blue": "#000080",
    "dark-magenta": "#800080",
    "dark-cyan": "#008080",
    "dark-grey": "#808080",
}

RTL_LANGUAGES = {"he", "heb", "iw", "ar", "ara", "fa", "fas", "per"}
ENGLISH = {"en", "eng"}

# Refuse a result that loses more than this share of the cues (e.g. HI removal on a
# subtitle that is mostly sound descriptions): that needs a person.
MAX_REMOVAL_RATIO = 0.5


# --- configuration ------------------------------------------------------------------


def parse(spec):
    """``"common, color(name=yellow)"`` -> ``([("common", {}), ("color", {...})], unknown)``.

    Names match Bazarr's, ignoring case. Returns the known mods in run order.
    """
    by_lower = {k.lower(): k for k in MODS}
    chosen, unknown = {}, []
    for raw in re.findall(r"[^,(]+(?:\([^)]*\))?", spec or ""):
        item = raw.strip()
        if not item:
            continue
        m = re.fullmatch(r"(\w+)(?:\((.*)\))?", item)
        mod_id = by_lower.get(m.group(1).lower()) if m else None
        if mod_id is None:
            unknown.append(item)
            continue
        args = dict(kv.split("=", 1) for kv in (m.group(2) or "").split(",") if "=" in kv)
        args = {k.strip(): v.strip() for k, v in args.items()}
        if mod_id == "color" and args.get("name", "").lower() not in COLORS:
            unknown.append(item)
            continue
        chosen[mod_id] = args
    return [(k, chosen[k]) for k in MODS if k in chosen], unknown


# --- running -------------------------------------------------------------------------


@dataclass
class ModResult:
    changes: dict = field(default_factory=dict)  # mod id -> cues changed or removed
    args: dict = field(default_factory=dict)  # mod id -> its arguments, e.g. color's name
    cues_before: int = 0
    cues_after: int = 0
    error: str | None = None

    @property
    def success(self):
        return self.error is None

    @property
    def changed(self):
        return any(self.changes.values())


def run(path, mods, sub_type=SubType.REGULAR, language="", write=False):
    """Apply ``mods`` (``[(id, args)]``, as from ``parse``) to the subtitle at ``path``.

    With ``write=False`` only reports what would change. Cues that end up empty are
    dropped; times are never changed.
    """
    try:
        with open(path, "rb") as f:
            decoded = decode_subtitle(f.read().removeprefix(UTF8_BOM))
        if decoded is None:
            return ModResult(error="could not detect the encoding")
        cues = S.parse(ftfy.fix_encoding(decoded[0]))
    except (OSError, ValueError) as e:
        return ModResult(error=str(e))

    language = _language(language, path)
    result = ModResult(cues_before=len(cues), args={k: a for k, a in mods if a})
    uppercase = mostly_uppercase(cues)
    for mod_id, args in mods:
        if mod_id == "remove_HI" and sub_type == SubType.HI:
            continue
        if mod_id == "reverse_rtl" and language not in RTL_LANGUAGES:
            continue
        if mod_id == "OCR_fixes" and language not in ENGLISH:
            continue  # the rules are English; Bazarr also skips languages it has no list for
        if mod_id == "fix_uppercase" and not uppercase:
            continue
        func = _FUNCS[mod_id]
        kept, changed = [], 0
        for cue in cues:
            text = func(cue.text, language=language, uppercase=uppercase, **args)
            if text != cue.text:
                text = _tidy(text)
            if text != cue.text:
                changed += 1
            if text:
                kept.append(replace(cue, text=text))
        cues = kept
        if changed:
            result.changes[mod_id] = changed

    result.cues_after = len(cues)
    if result.cues_before and (
        result.cues_before - result.cues_after > result.cues_before * MAX_REMOVAL_RATIO
    ):
        result.error = (
            f"refusing to remove {result.cues_before - result.cues_after} of "
            f"{result.cues_before} cues (limit {MAX_REMOVAL_RATIO:.0%})"
        )
        return result
    if write and result.changed:
        atomic_write_text(path, S.compose(cues))
    return result


def summary(changes):
    """``{"common": 3, "remove_tags": 12}`` -> ``"remove tags (12 cues), common fixes (3)"``."""
    parts = []
    for mod_id in MODS:
        if changes.get(mod_id):
            n = changes[mod_id]
            parts.append(f"{MODS[mod_id].label} ({n} cue{'s' if n != 1 else ''})")
    return ", ".join(parts)


def _language(language, path):
    language = (language or "").split(":")[0].lower()
    if language:
        return language
    parts = os.path.basename(path).lower().split(".")[1:-1]
    for part in reversed(parts):
        if part in ("forced", "foreign", "hi", "sdh", "cc"):
            continue
        return part if len(part) in (2, 3) else ""
    return ""


def _tidy(text):
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if _TAG_RE.sub("", line).strip(" -")]
    return "\n".join(lines)


# --- the modifications ---------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")
_ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")


def _remove_tags(text, **_):
    return _TAG_RE.sub("", text)


def _emoji(text, **_):
    text = emoji_lib.replace_emoji(text, "")
    return "\n".join(re.sub(r" {2,}", " ", line).strip() for line in text.split("\n"))


# OCR: "l" read for "I" (and "|" for "I"), "0" read for "o" inside words.
_OCR_L_WORDS = {
    "l": "I",
    "l'm": "I'm",
    "l'll": "I'll",
    "l've": "I've",
    "l'd": "I'd",
    "lt": "It",
    "lt's": "It's",
    "lts": "Its",
    "lf": "If",
    "ln": "In",
    "lnto": "Into",
    "ls": "Is",
    "lsn't": "Isn't",
}
_WORD_RE = re.compile(r"(?<![\w'])[\w|][\w'|]*")
_ZERO_IN_WORD_RE = re.compile(r"(?<=[a-z])0(?=[a-z])|(?<=\b[A-Z])0(?=[a-z])")
_L_BEFORE_CONSONANT_RE = re.compile(r"^l(?=[bcdfghjkmnpqrstvwxz][a-z])")


def _ocr_fixes(text, **_):
    def word(m):
        w = m.group(0).replace("|", "I")
        if w in _OCR_L_WORDS:
            return _OCR_L_WORDS[w]
        if w.lower() not in ("lb", "lbs"):
            w = _L_BEFORE_CONSONANT_RE.sub("I", w)  # "lmpossible" -> "Impossible"
        return w

    text = _WORD_RE.sub(word, text)
    return _ZERO_IN_WORD_RE.sub("o", text)


_SOUND_WORDS = (
    "LAUGH",
    "APPLAU",
    "CHEER",
    "MUSIC",
    "GASP",
    "SIGH",
    "GROAN",
    "COUGH",
    "SCREAM",
    "SHOUT",
    "WHISPER",
    "PHONE",
    "DOOR",
    "KNOCK",
    "FOOTSTEP",
    "THUNDER",
    "EXPLOSION",
    "GUNSHOT",
    "SIREN",
)
_MUSIC = "♪♫#*¶"
_FULL_BRACKET_RE = re.compile(r"^-?\s*(\[[^\]]{3,}\]|\([^)]{3,}\))\s*:?$")
_INLINE_BRACKET_RE = re.compile(r"\s*\[[^\]]{3,}\]\s*:?")
_CAPS_SPEAKER_RE = re.compile(r"^(-\s*)?[A-Z][A-Z0-9 .'&#-]{2,}(?: \([^)]*\))?:\s+(?=\S)(?!\d)")
_NAME_SPEAKER_RE = re.compile(r"^(-\s*)?[A-Z][a-z]{2,15}:\s+(?=[A-Z\"'¿¡])")
_CAPS_LINE_RE = re.compile(r"^[A-Z\s&+'.,!-]+$")
_CAPS_LEAD_RE = re.compile(r"^(-\s*)?[A-Z]{4,}(?:\s+[A-Z]{2,})*\s+(?=[A-Z][a-z])")
_MAN_RE = re.compile(r"\b(?:WO)?MAN:\s*")


def _remove_hi(text, uppercase=False, **_):
    lines = text.split("\n")
    plain = [_ASS_TAG_RE.sub("", _TAG_RE.sub("", line)).strip() for line in lines]
    if all(_FULL_BRACKET_RE.match(p) or not p for p in plain):
        return ""
    out = []
    for raw, bare in zip(lines, plain, strict=True):
        if _FULL_BRACKET_RE.match(bare):
            continue
        if bare and (bare[0] in _MUSIC or bare[-1] in _MUSIC):
            continue  # lyrics and music cues: "♪ Fly me to the moon ♪"
        line = _INLINE_BRACKET_RE.sub(" ", raw).strip()
        if not uppercase:
            if _CAPS_LINE_RE.match(bare) and any(w in bare for w in _SOUND_WORDS):
                continue
            line = _CAPS_LEAD_RE.sub(lambda m: m.group(1) or "", line)
        line = _CAPS_SPEAKER_RE.sub(lambda m: m.group(1) or "", line)
        line = _NAME_SPEAKER_RE.sub(lambda m: m.group(1) or "", line)
        line = _MAN_RE.sub("", line)
        out.append(line)
    out = [line for line in out if line.strip(" -")]
    if len(out) == 1 and len(lines) > 1:
        out[0] = re.sub(r"^-\s*", "", out[0])  # one speaker left: no dialogue dash
    return "\n".join(out)


_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "no", "mt", "lt", "sgt", "etc"}
_COMMON_RULES = [
    (re.compile(r"[‑‐﹘﹣]"), "-"),  # odd hyphens
    (re.compile(r"-\s?--?(?=\s|$)|(?<=\w)--(?=\w)"), "—"),  # -- as a dash
    (re.compile(r"^\s?>>\s*"), ""),  # leading >>
    (re.compile(r"^\W*:\s*(?=\w)"), ""),  # leading colon
    (re.compile(r"(?i).+downloaded\s+from.+"), ""),
    (re.compile(r"['’ʼ❜‘‛]['’ʼ❜‘‛]+"), '"'),  # '' -> "
    (re.compile(r"[”“‟„]"), '"'),
    (re.compile(r"[’ʼ❜‘‛]"), "'"),
    (re.compile(r"(?<=[A-Za-zÀ-ž])\"(?=[A-Za-zÀ-ž])"), "'"),  # don"t -> don't
    (re.compile(r"^\.\.\.\s*"), ""),  # leading ellipsis
    (re.compile(r"\.{4,}"), "..."),
    (re.compile(r"\.\.\.(?![\s.,!?'\"]|$)"), "... "),
    (re.compile(r"^-(?![\s-])"), "- "),
    (re.compile(r"(?<=\w) +(?=[!?.,](?![!?.,]))"), ""),  # space before punctuation
    (re.compile(r"\s{2,}"), " "),
]
_DOUBLE_PUNCT_RE = re.compile(r"([,!?])[,.]+(?!\.)")
_NO_SPACE_AFTER_RE = re.compile(r"(\b[A-Za-zÀ-ž]{2,})([!?,]|\.(?!\.))([A-Za-zÀ-ž]{2,})")
_URL_RE = re.compile(r"https?://|www\.|\w\.(com|org|net|io|tv|co\.uk)\b", re.IGNORECASE)
_I_IN_WORD_RE = re.compile(r"\b(?!Mc|Mac)([A-Za-z]?[a-z]+)(I+)(?=[a-z])")
_AFTER_DOT_RE = re.compile(r"\b([A-Za-zÀ-ž]{2,})\.(\s+)([a-zà-ž])")
_LOWER_I_RE = re.compile(r"\bi\b(?!\.)")


def _common(text, language="", **_):
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if re.fullmatch(r"\W*[-_.:<>~\"']+\W*", line) and not any(c in line for c in _MUSIC):
            continue
        for pattern, repl in _COMMON_RULES:
            line = pattern.sub(repl, line)
        if language not in ("es", "spa"):  # "¿¡...?!" is correct Spanish
            line = _DOUBLE_PUNCT_RE.sub(r"\1", line)
        if not _URL_RE.search(line):
            line = _NO_SPACE_AFTER_RE.sub(r"\1\2 \3", line)
        line = _I_IN_WORD_RE.sub(lambda m: m.group(1) + "l" * len(m.group(2)), line)
        line = _AFTER_DOT_RE.sub(
            lambda m: (
                m.group(0)
                if m.group(1).lower() in _ABBREVIATIONS
                else f"{m[1]}.{m[2]}{m[3].upper()}"
            ),
            line,
        )
        if language in ENGLISH:
            line = _LOWER_I_RE.sub("I", line)
        lines.append(line.strip())
    return "\n".join(lines)


# Sentence ends, music notes and dialogue dashes; not hyphens inside words ("well-known").
_SENTENCE_SPLIT_RE = re.compile(r"(\s*[.!?♪]\s*|^\s*-\s*|\s+-\s+)")


def _fix_uppercase(text, language="", **_):
    def sentence(part):
        stripped = part.lstrip()
        lead = part[: len(part) - len(stripped)]
        return lead + stripped[:1].upper() + stripped[1:].lower()

    text = "\n".join(
        "".join(sentence(p) for p in _SENTENCE_SPLIT_RE.split(line)) for line in text.split("\n")
    )
    if language in ENGLISH:
        text = re.sub(r"\bi\b", "I", text)
    return text


_RTL_RE = re.compile(r"^([\s.!?:,'-]*)(.+?)(\s*)(-?\s*)$")
_RTL_BROKEN_RE = re.compile(r"^\s*[.!?:,]")


def _reverse_rtl(text, **_):
    # Only lines that still start with their closing punctuation, so a line that is
    # already right is never flipped back on a later check.
    return "\n".join(
        _RTL_RE.sub(r"\4\3\2\1", line) if _RTL_BROKEN_RE.match(line) else line
        for line in text.split("\n")
    )


_COLOR_RE = re.compile(r'^<font color="#[0-9A-Fa-f]{6}">(.*)</font>$', re.DOTALL)


def _color(text, name="", **_):
    m = _COLOR_RE.match(text)  # already coloured: recolour, don't nest
    return f'<font color="{COLORS[name.lower()]}">{m.group(1) if m else text}</font>'


_FUNCS = {
    "remove_tags": _remove_tags,
    "emoji": _emoji,
    "OCR_fixes": _ocr_fixes,
    "remove_HI": _remove_hi,
    "common": _common,
    "fix_uppercase": _fix_uppercase,
    "reverse_rtl": _reverse_rtl,
    "color": _color,
}


def mostly_uppercase(cues):
    """Bazarr's test: in the first 50 cues with text (sound descriptions aside), at least
    90 % of the letters are uppercase, and there are more than 100 of them."""
    upper = lower = seen = 0
    for cue in cues:
        text = _INLINE_BRACKET_RE.sub(" ", _TAG_RE.sub("", cue.text))
        text = re.sub(r"\([^)]*\)", " ", text).strip()
        if not text:
            continue
        upper += sum(c.isupper() for c in text)
        lower += sum(c.islower() for c in text)
        seen += 1
        if seen >= 50:
            break
    return upper > 100 and upper * 100 >= 90 * (upper + lower)
