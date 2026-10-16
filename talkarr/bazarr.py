import logging

import requests

from .config import Config

log = logging.getLogger(__name__)


class BazarrClient:
    def __init__(self):
        self.url = Config.BAZARR_URL.rstrip("/")
        self.api_key = Config.BAZARR_API_KEY

    def _get(self, path, params=None):
        r = requests.get(
            f"{self.url}/api{path}",
            headers={"X-API-KEY": self.api_key},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, path, params=None):
        r = requests.delete(
            f"{self.url}/api{path}",
            headers={"X-API-KEY": self.api_key},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r

    def _post(self, path, json=None):
        r = requests.post(
            f"{self.url}/api{path}",
            headers={"X-API-KEY": self.api_key},
            json=json,
            timeout=30,
        )
        r.raise_for_status()
        return r

    def _patch(self, path, json=None):
        r = requests.patch(
            f"{self.url}/api{path}",
            headers={"X-API-KEY": self.api_key},
            json=json,
            timeout=30,
        )
        r.raise_for_status()
        return r

    def get_movie_subtitles(self, radarr_id):
        return self._get(f"/movies", params={"radarrid[]": radarr_id}).get("data", [])

    def get_episode_subtitles(self, sonarr_episode_id):
        return self._get(f"/episodes",
                         params={"episodeid[]": sonarr_episode_id}).get("data", [])

    def delete_movie_subtitle(self, radarr_id, language, forced, hi, path):
        self._delete(f"/movies/subtitles", params={
            "radarrid": radarr_id,
            "language": language,
            "forced": forced,
            "hi": hi,
            "path": path,
        })
        log.info("Deleted movie subtitle: radarr_id=%s path=%s",
                 radarr_id, path)

    def delete_episode_subtitle(self, sonarr_series_id, sonarr_episode_id,
                                language, forced, hi, path):
        self._delete(f"/episodes/subtitles", params={
            "seriesid": sonarr_series_id,
            "episodeid": sonarr_episode_id,
            "language": language,
            "forced": forced,
            "hi": hi,
            "path": path,
        })
        log.info("Deleted episode subtitle: episode_id=%s path=%s",
                 sonarr_episode_id, path)

    def search_movie_subtitles(self, radarr_id):
        self._patch(f"/movies/subtitles", json={
            "radarrid": radarr_id,
            "language": "en",
        })
        log.info("Triggered movie subtitle search: radarr_id=%s", radarr_id)

    def search_episode_subtitles(self, sonarr_episode_id):
        self._patch(f"/episodes/subtitles", json={
            "episodeid": sonarr_episode_id,
            "language": "en",
        })
        log.info("Triggered episode subtitle search: episode_id=%s",
                 sonarr_episode_id)

    def blacklist_movie_subtitle(self, provider, subs_id, language, radarr_id):
        self._post(f"/providers/movies/blacklist", json={
            "provider": provider,
            "subs_id": subs_id,
            "language": language,
            "radarrid": radarr_id,
        })
        log.info("Blacklisted movie subtitle: radarr_id=%s", radarr_id)

    def blacklist_episode_subtitle(self, provider, subs_id, language,
                                   sonarr_series_id, sonarr_episode_id):
        self._post(f"/providers/episodes/blacklist", json={
            "provider": provider,
            "subs_id": subs_id,
            "language": language,
            "seriesid": sonarr_series_id,
            "episodeid": sonarr_episode_id,
        })
        log.info("Blacklisted episode subtitle: episode_id=%s",
                 sonarr_episode_id)
