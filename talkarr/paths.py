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

    for entry in raw.split("|"):
        pair = entry.strip()
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
        # Match whole path segments only: "/data" must not match "/database".
        if path == src or path.startswith(src + "/"):
            return dst + path[len(src) :]

    return path


def to_source_path(path):
    """The reverse of ``translate_path``: our path as the source container (Bazarr) sees it.

    "/data:/media" means our /media/movies/... → Bazarr's /data/movies/...
    """
    if not path:
        return path

    for src, dst in _parse_mappings():
        if path == dst or path.startswith(dst + "/"):
            return src + path[len(dst) :]

    return path
