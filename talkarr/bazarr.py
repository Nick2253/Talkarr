"""Talking to Bazarr: find who downloaded a subtitle, and blacklist it.

When Talkarr removes a subtitle that Bazarr downloaded, deleting the file is not
enough: Bazarr sees the subtitle missing and fetches the same top-scoring file
again (observed: one forced subtitle re-downloaded three times in five days).
Bazarr's blacklist call deletes the file, records provider + subtitle ID as bad,
and searches again, which is what a person does from Bazarr's history page.

API (Bazarr 1.6): GET /api/movies, /api/series, /api/episodes?seriesid[]=,
/api/{movies,episodes}/history?{radarrid,episodeid}=; POST
/api/{movies,episodes}/blacklist (form: radarrid | seriesid+episodeid, provider,
subs_id, language (code2), subtitles_path). Paths are in Bazarr's namespace.
"""

import logging
import os
import time
from dataclasses import dataclass

import requests

from .config import Config
from .paths import to_source_path

log = logging.getLogger(__name__)

CACHE_SECONDS = 600


class BazarrError(Exception):
    pass


@dataclass(frozen=True)
class Download:
    """Bazarr's record of having downloaded one subtitle file."""

    kind: str  # "movie" or "episode"
    ids: dict  # {"radarrid": n} or {"seriesid": n, "episodeid": n}
    provider: str
    subs_id: str
    language: str  # code2, as Bazarr's blacklist expects
    subtitles_path: str  # Bazarr's path


def enabled():
    return bool(Config.BAZARR_BLACKLIST and Config.BAZARR_URL and Config.BAZARR_API_KEY)


class Bazarr:
    # Bazarr answers slowly while it searches providers (after blacklisting, say).
    def __init__(self, url=None, api_key=None, timeout=90):
        self.url = (url or Config.BAZARR_URL).rstrip("/")
        self.headers = {"X-API-KEY": api_key or Config.BAZARR_API_KEY}
        self.timeout = timeout
        self._cache = {}

    # --- HTTP ---------------------------------------------------------------------------

    def _get(self, path, params=None):
        try:
            r = requests.get(
                f"{self.url}/api/{path}", headers=self.headers, params=params, timeout=self.timeout
            )
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise BazarrError(f"Bazarr {path}: {e}") from e

    def _cached(self, key, loader):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = loader()
        self._cache[key] = (time.monotonic(), value)
        return value

    # --- lookups --------------------------------------------------------------------------

    def find_download(self, subtitle_path, video_path):
        """Bazarr's latest download record for this exact subtitle file, or None."""
        bazarr_sub = to_source_path(subtitle_path)
        bazarr_video = to_source_path(video_path) if video_path else None
        media = self._find_media(bazarr_video, os.path.dirname(bazarr_sub))
        if media is None:
            return None
        kind, ids = media
        if kind == "movie":
            rows = self._get("movies/history", {"radarrid": ids["radarrid"], "length": -1})
        else:
            rows = self._get("episodes/history", {"episodeid": ids["episodeid"], "length": -1})
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        for row in sorted(rows, key=lambda r: r.get("timestamp") or 0, reverse=True):
            if row.get("subtitles_path") != bazarr_sub:
                continue
            if not (row.get("provider") and row.get("subs_id")) or row.get("blacklisted"):
                continue
            language = row.get("language") or {}
            code2 = language.get("code2") if isinstance(language, dict) else str(language)
            return Download(
                kind, ids, row["provider"], str(row["subs_id"]), code2 or "en", bazarr_sub
            )
        return None

    def _find_media(self, bazarr_video, bazarr_dir):
        movies = self._cached("movies", lambda: self._data(self._get("movies", {"length": -1})))
        for m in movies:
            path = m.get("path") or ""
            if path == bazarr_video or os.path.dirname(path) == bazarr_dir:
                return "movie", {"radarrid": m["radarrId"]}
        series = self._cached("series", lambda: self._data(self._get("series", {"length": -1})))
        for s in series:
            root = (s.get("path") or "").rstrip("/")
            if root and (bazarr_dir == root or bazarr_dir.startswith(root + "/")):
                episodes = self._data(self._get("episodes", {"seriesid[]": s["sonarrSeriesId"]}))
                for e in episodes:
                    if e.get("path") == bazarr_video:
                        return "episode", {
                            "seriesid": s["sonarrSeriesId"],
                            "episodeid": e["sonarrEpisodeId"],
                        }
        return None

    @staticmethod
    def _data(payload):
        return payload.get("data", []) if isinstance(payload, dict) else payload

    # --- actions ---------------------------------------------------------------------------

    def blacklist(self, download):
        """Ask Bazarr to blacklist the subtitle: it deletes the file and searches again."""
        form = {
            **download.ids,
            "provider": download.provider,
            "subs_id": download.subs_id,
            "language": download.language,
            "subtitles_path": download.subtitles_path,
        }
        endpoint = "movies/blacklist" if download.kind == "movie" else "episodes/blacklist"
        try:
            r = requests.post(
                f"{self.url}/api/{endpoint}", headers=self.headers, data=form, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise BazarrError(f"Bazarr blacklist: {e}") from e
        if r.status_code != 200:
            raise BazarrError(f"Bazarr refused to blacklist it ({r.status_code}: {r.text[:200]})")
        log.info(
            "Blacklisted in Bazarr: %s %s %s",
            download.provider,
            download.subs_id,
            download.subtitles_path,
        )
