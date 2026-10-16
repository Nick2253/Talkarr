import logging

import requests

from .config import Config

log = logging.getLogger(__name__)


class SonarrClient:
    def __init__(self):
        self.url = Config.SONARR_URL.rstrip("/")
        self.api_key = Config.SONARR_API_KEY

    def _get(self, path, params=None):
        r = requests.get(
            f"{self.url}/api/v3{path}",
            headers={"X-Api-Key": self.api_key},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_series(self, series_id):
        return self._get(f"/series/{series_id}")

    def get_series_language(self, series_id):
        series = self.get_series(series_id)
        lang = series.get("originalLanguage", {})
        return lang.get("name", "").lower()

    def get_episode_by_file(self, file_path):
        """Find an episode whose file path matches."""
        episodes = self._get("/episode", params={"includeEpisodeFile": True})
        for ep in episodes:
            ef = ep.get("episodeFile", {})
            if ef and ef.get("path") == file_path:
                return ep
        return None
