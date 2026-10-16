import logging
import os
import re

import pysrt
import charset_normalizer

log = logging.getLogger(__name__)

AD_PATTERNS = [
    r"opensubtitles",
    r"subscene",
    r"addic7ed",
    r"podnapisi",
    r"subdivx",
    r"yifysubtitles",
    r"subtitles?\s*by\b",
    r"ripped\s*by\b",
    r"synced?\s*(&|and)\s*corrected\s*by\b",
    r"advertise\s+your\s+product",
    r"support\s+us\s+and\s+become\s+vip",
    r"please\s+rate\s+this\s+subtitle",
    r"a]]",
    r"www\.\S+\.(com|org|net|io)",
    r"https?://\S+",
    r"captioned\s+by\b",
    r"encoded\s+by\b",
    r"subtitling\s+by\b",
]

AD_RE = re.compile("|".join(AD_PATTERNS), re.IGNORECASE)

MOJIBAKE_PATTERNS = [
    "Ã¡", "Ã©", "Ã­",
    "Ã³", "Ãº", "Ã±",
    "Ã¼", "Ã¶", "Ã¤",
    "â\u0080\u0099", "â\u0080\u009c",
    "â\u0080\u009d", "â\u0080\u0093",
    "â\u0080\u0094",
    "Â", "Ã", "Å",
]

MOJIBAKE_RE = re.compile("|".join(re.escape(p) for p in MOJIBAKE_PATTERNS))


class CleanResult:
    def __init__(self, ads_removed=0, encoding_fixed=False,
                 bom_stripped=False, cues_before=0, cues_after=0,
                 error=None):
        self.ads_removed = ads_removed
        self.encoding_fixed = encoding_fixed
        self.bom_stripped = bom_stripped
        self.cues_before = cues_before
        self.cues_after = cues_after
        self.error = error

    @property
    def success(self):
        return self.error is None

    @property
    def changed(self):
        return self.ads_removed > 0 or self.encoding_fixed or self.bom_stripped


def clean_subtitle(subtitle_path):
    """Clean a subtitle file: fix encoding, strip BOM, remove ads."""
    if not os.path.isfile(subtitle_path):
        return CleanResult(error=f"File not found: {subtitle_path}")

    try:
        encoding_fixed = False
        bom_stripped = False

        raw = open(subtitle_path, "rb").read()

        if raw[:3] == b"\xef\xbb\xbf":
            raw = raw[3:]
            bom_stripped = True

        text = _decode_with_fallback(raw)
        if text is None:
            return CleanResult(error="Could not detect encoding")

        if MOJIBAKE_RE.search(text):
            fixed = _fix_mojibake(raw)
            if fixed and not MOJIBAKE_RE.search(fixed):
                text = fixed
                encoding_fixed = True

        subs = pysrt.from_string(text)
        cues_before = len(subs)

        ads_removed = 0
        to_remove = []
        for i, sub in enumerate(subs):
            if _is_ad(sub, i, cues_before):
                to_remove.append(i)
                ads_removed += 1

        for i in reversed(to_remove):
            del subs[i]

        subs.clean_indexes()
        cues_after = len(subs)

        result = CleanResult(
            ads_removed=ads_removed,
            encoding_fixed=encoding_fixed,
            bom_stripped=bom_stripped,
            cues_before=cues_before,
            cues_after=cues_after,
        )

        if result.changed:
            subs.save(subtitle_path, encoding="utf-8")
            log.info("Cleaned %s: %d ads removed, encoding_fixed=%s",
                     os.path.basename(subtitle_path), ads_removed,
                     encoding_fixed)

        return result

    except Exception as e:
        return CleanResult(error=str(e))


def _is_ad(sub, index, total):
    text = sub.text.strip()
    if not text:
        return True

    if AD_RE.search(text):
        return True

    if (index <= 1 or index >= total - 2) and len(text) < 200:
        plain = re.sub(r"<[^>]+>", "", text).strip()
        if re.match(r"^[\W\s]*$", plain):
            return True

    return False


def _decode_with_fallback(raw):
    for enc in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue

    result = charset_normalizer.from_bytes(raw).best()
    if result:
        return str(result)

    for enc in ("latin-1", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue

    return None


def _fix_mojibake(raw):
    try:
        text = raw.decode("latin-1")
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    try:
        text = raw.decode("cp1252")
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    return None
