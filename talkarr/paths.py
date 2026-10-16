import logging

from .config import Config

log = logging.getLogger(__name__)

_mappings = None


def _parse_mappings():
    global _mappings
    if _mappings is not None:
        return _mappings

    _mappings = []
    raw = Config.PATH_MAPPING
    if not raw:
        return _mappings

    for pair in raw.split("|"):
        pair = pair.strip()
        if ":" not in pair:
            continue
        src, dst = pair.split(":", 1)
        _mappings.append((src.rstrip("/"), dst.rstrip("/")))

    if _mappings:
        log.info("Path mappings: %s", _mappings)

    return _mappings


def translate_path(path):
    """Translate a path from the source container's namespace to ours.

    PATH_MAPPING format: "/source/path:/local/path|/other:/mapped"
    Example: "/data:/media" means Bazarr's /data/movies/... → /media/movies/...
    """
    if not path:
        return path

    for src, dst in _parse_mappings():
        if path.startswith(src):
            translated = dst + path[len(src):]
            return translated

    return path
