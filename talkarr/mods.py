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
        context = {"language": language, "uppercase": uppercase, **args}
        if mod_id == "remove_HI":
            context.update(_hi_style(cues))
        kept, changed = [], 0
        for cue in cues:
            text = func(cue.text, **context)
            if text != cue.text:
                text = _tidy(text)
                if (
                    mod_id != "common"
                    and _SPACES_RE.sub(" ", text) == _SPACES_RE.sub(" ", cue.text).strip()
                ):
                    text = cue.text  # only whitespace moved: that's common fixes' job
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
    """Trim lines, drop ones left with nothing but tags or a dash, and repair tags."""
    lines = [line.strip() for line in _balance_tags(text).split("\n")]
    lines = [line for line in lines if _TAG_RE.sub("", line).strip(" -")]
    # Spaces left just inside tags at a line's ends: "<i>I was... </i>" -> "<i>I was...</i>".
    lines = [re.sub(r"^((?:<(?!/)[^>]*>)+)\s+", r"\1", line) for line in lines]
    lines = [re.sub(r"\s+((?:</\w+>)+)$", r"\1", line) for line in lines]
    return "\n".join(lines)


_SPACES_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")
_HTML_TAG_RE = re.compile(r"<(/?)(i|b|u|font)\b[^>]*>", re.IGNORECASE)


def _balance_tags(text):
    """Drop closing tags with no opening one and close what was left open (a removed
    line can take half of an ``<i>...</i>`` pair with it)."""
    out, stack, pos = [], [], 0
    for m in _HTML_TAG_RE.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        name = m.group(2).lower()
        if not m.group(1):
            stack.append(name)
            out.append(m.group(0))
        elif name in stack:
            while stack:  # close anything opened inside it first
                inner = stack.pop()
                out.append(m.group(0) if inner == name else f"</{inner}>")
                if inner == name:
                    break
    out.append(text[pos:])
    out.extend(f"</{name}>" for name in reversed(stack))
    return "".join(out)


def _outside_tags(func, line):
    """Apply ``func`` to the text of ``line`` between tags, leaving the tags alone."""
    parts = re.split(f"({_TAG_RE.pattern})", line)
    return "".join(p if i % 2 else func(p) for i, p in enumerate(parts))


def _plain(line):
    return _TAG_RE.sub("", line).strip()


# --- the modifications ---------------------------------------------------------------


def _remove_tags(text, **_):
    return _TAG_RE.sub("", text)


_KEEP_SYMBOLS = "©®™"  # emoji to the emoji package, but ordinary text in subtitles


def _emoji(text, **_):
    marked = emoji_lib.replace_emoji(
        text, replace=lambda chars, _data: chars if chars in _KEEP_SYMBOLS else "\0"
    )
    if marked == text:
        return text
    return "\n".join(re.sub(r" *\0+ *", " ", line).strip() for line in marked.split("\n"))


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
# Not "l-l-let's" or "l-o-v-e": a stutter or spelling-out, not "I".
_WORD_RE = re.compile(r"(?<![\w'|-])[\w|][\w'|]*+(?!-[a-z])")
_ZERO_IN_WORD_RE = re.compile(r"(?<=[a-z])0(?=[a-z])|(?<=\b[A-Z])0(?=[a-z])")
_L_BEFORE_CONSONANT_RE = re.compile(r"^l(?=[bcdfghjkmnpqrstvwxz][a-z])")
# A lone "|" is "I" only where "I" reads on ("| can't", "|'m", "|-I"), not between
# capitalised words, where it is a separator ("Idiot | Moron").
_LONE_PIPE_RE = re.compile(r"(?<![^\s\"'(-])\|(?=\s+[a-z]|['’-]|$)")


def _ocr_fixes(text, **_):
    def word(m):
        w = m.group(0)
        if w == "|":
            return w
        if w[0] == "|" and (w[1].islower() or w[1] == "'"):
            w = "I" + w[1:]  # "|t's"; not "that|" or "|If"
        if w in _OCR_L_WORDS:
            return _OCR_L_WORDS[w]
        if w.lower() not in ("lb", "lbs"):
            w = _L_BEFORE_CONSONANT_RE.sub("I", w)  # "lmpossible" -> "Impossible"
        return w

    def fix(segment):
        segment = _LONE_PIPE_RE.sub("I", segment)
        segment = _WORD_RE.sub(word, segment)
        return _ZERO_IN_WORD_RE.sub("o", segment)

    return "\n".join(_outside_tags(fix, line) for line in text.split("\n"))


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
_MUSIC = "♪♫#¶"  # not "*": "Oh, boy.***" is dialogue
_FULL_BRACKET_RE = re.compile(r"^-?\s*(\[[^\]]{3,}\]|\([^)]{3,}\))\s*:?$")
_INLINE_BRACKET_RE = re.compile(r"\s*\[[^\]]{3,}\]\s*:?")
_CAPS_SPEAKER_RE = re.compile(r"^(-\s*)?[A-Z][A-Z0-9 .'&#-]{2,}(?: \([^)]*\))?:\s+(?=\S)(?!\d)")
_NAME_SPEAKER_RE = re.compile(r"^(-\s*)?([A-Z][a-z]{2,15}):\s+(?=[A-Z\"'¿¡])")
_CAPS_LINE_RE = re.compile(r"^[A-Z\s&+'.,!-]+$")
_CAPS_LEAD_RE = re.compile(r"^(-\s*)?[A-Z]{4,}(?:\s+[A-Z]{2,})*\s+(?=[A-Z][a-z])")
_MAN_RE = re.compile(r"\b(?:WO)?MAN:\s*")
# A file must show this many cues in a style before the riskier HI rules trust it.
HI_STYLE_MIN_CUES = 5


def _hi_style(cues):
    """What this subtitle's HI text looks like, so rules that would misfire on ordinary
    dialogue ("Repeat: Abort!", "NCIS Marseille is...") only run on files written that way.
    """
    names, labelled, hi_cues = set(), 0, 0
    for cue in cues:
        lines = [_plain(line) for line in cue.text.split("\n")]
        found = [m.group(2) for line in lines if (m := _NAME_SPEAKER_RE.match(line))]
        if found:
            labelled += 1
            names.update(found)
        if any(
            _FULL_BRACKET_RE.match(line)
            or _INLINE_BRACKET_RE.search(line)
            or _CAPS_SPEAKER_RE.match(line)
            or _is_sound_line(line)
            for line in lines
        ):
            hi_cues += 1
    return {
        "speaker_names": labelled >= HI_STYLE_MIN_CUES and len(names) >= 2,
        "hi_styled": hi_cues >= HI_STYLE_MIN_CUES,
    }


def _is_sound_line(plain):
    """``CHRIS LAUGHS``, ``DOOR SLAMS``: a short all-caps line naming a sound."""
    words = plain.split()
    return (
        bool(_CAPS_LINE_RE.match(plain))
        and 0 < len(words) <= 4
        and any(w.startswith(s) for w in words for s in _SOUND_WORDS)
    )


def _is_music(plain):
    return bool(plain) and (plain[0] in _MUSIC or plain[-1] in _MUSIC)


def _remove_hi(text, uppercase=False, speaker_names=False, hi_styled=False, **_):
    lines = text.split("\n")
    plain = [_plain(line) for line in lines]
    if all(_FULL_BRACKET_RE.match(p) or not p for p in plain):
        return ""

    # Lyrics: a line opening with a music note runs on until the next speaker's dash;
    # one closing with a note runs back to the previous one.
    music = [False] * len(lines)
    for i, p in enumerate(plain):
        if p and p[0] in _MUSIC:
            j = i
            while j < len(lines) and (j == i or not plain[j].startswith("-")):
                music[j] = True
                j += 1
        if p and p[-1] in _MUSIC:
            j = i
            while j >= 0:
                music[j] = True
                if plain[j].startswith("-"):
                    break
                j -= 1

    out = []
    for i, raw in enumerate(lines):
        bare = plain[i]
        if music[i] or _FULL_BRACKET_RE.match(bare):
            continue
        line = _INLINE_BRACKET_RE.sub(" ", raw).strip()
        if not uppercase:
            if _is_sound_line(bare):
                continue
            if hi_styled:
                line = _CAPS_LEAD_RE.sub(lambda m: m.group(1) or "", line)
        line = _CAPS_SPEAKER_RE.sub(lambda m: m.group(1) or "", line)
        if speaker_names:
            line = _NAME_SPEAKER_RE.sub(lambda m: m.group(1) or "", line)
        line = _MAN_RE.sub("", line)
        out.append(line)
    out = [line for line in out if _plain(line).strip(" -")]
    if len(out) == 1 and len(lines) > 1:
        out[0] = re.sub(r"^((?:<[^>]*>)*)-\s*", r"\1", out[0])  # one speaker left: no dash
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
    def fix(segment, first):
        for pattern, repl in _COMMON_RULES:
            if first or not pattern.pattern.startswith("^"):
                segment = pattern.sub(repl, segment)
        if language not in ("es", "spa"):  # "¿¡...?!" is correct Spanish
            segment = _DOUBLE_PUNCT_RE.sub(r"\1", segment)
        if not _URL_RE.search(segment):
            segment = _NO_SPACE_AFTER_RE.sub(r"\1\2 \3", segment)
        segment = _I_IN_WORD_RE.sub(lambda m: m.group(1) + "l" * len(m.group(2)), segment)
        segment = _AFTER_DOT_RE.sub(
            lambda m: (
                m.group(0)
                if m.group(1).lower() in _ABBREVIATIONS
                else f"{m[1]}.{m[2]}{m[3].upper()}"
            ),
            segment,
        )
        if language in ENGLISH:
            segment = _LOWER_I_RE.sub("I", segment)
        return segment

    lines = []
    for raw in text.split("\n"):
        bare = _plain(raw)
        if re.fullmatch(r"\W*[-_.:<>~\"']+\W*", bare) and not any(c in bare for c in _MUSIC):
            continue
        # Line-start rules apply to the first text after any opening tags ("<i>-Foul!").
        parts = re.split(f"({_TAG_RE.pattern})", raw.strip())
        first = next((i for i, p in enumerate(parts) if i % 2 == 0 and p.strip()), None)
        lines.append(
            "".join(
                p if i % 2 else fix(p.lstrip() if i == first else p, i == first)
                for i, p in enumerate(parts)
            ).strip()
        )
    return "\n".join(lines)


def _fix_uppercase(text, language="", **_):
    """Sentence case, outside tags: a capital after ``.!?♪``, a dialogue dash or a
    speaker label, lower case everywhere else."""
    out, capital, in_tag, line_start = [], True, None, True
    for i, ch in enumerate(text):
        if in_tag:
            out.append(ch)
            if ch == in_tag:
                in_tag = None
            continue
        if ch in "<{":
            in_tag = ">" if ch == "<" else "}"
            out.append(ch)
            continue
        if ch.isalpha():
            initial = text[i + 1 : i + 2] == "." and not text[i - 1 : i].isalpha()
            out.append(ch.upper() if capital or initial else ch.lower())  # "George C. Marshall"
            capital = line_start = False
            continue
        out.append(ch)
        if ch in ".!?♪:":
            # An ellipsis runs on ("...as equals"); a full stop starts a sentence.
            capital = ch != "." or "." not in (text[i - 1 : i], text[i + 1 : i + 2])
        elif ch == "\n":
            line_start = True
        elif ch == "-" and line_start:
            capital = True
        elif not ch.isspace():
            capital = capital and ch in "\"'(¿¡"
    fixed = "".join(out)
    if language in ENGLISH:
        fixed = "\n".join(
            _outside_tags(lambda s: re.sub(r"\bi\b", "I", s), line) for line in fixed.split("\n")
        )
    return fixed


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
