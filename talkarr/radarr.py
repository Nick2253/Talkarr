import logging

import requests

from .config import Config

log = logging.getLogger(__name__)


class RadarrClient:
    def __init__(self):
        self.url = Config.RADARR_URL.rstrip("/")
        self.api_key = Config.RADARR_API_KEY

    def _get(self, path, params=None):
        r = requests.get(
            f"{self.url}/api/v3{path}",
            headers={"X-Api-Key": self.api_key},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_movie_by_path(self, file_path):
        """Find a movie whose file path matches."""
        movies = self._get("/movie")
        for movie in movies:
            mf = movie.get("movieFile", {})
            if mf and mf.get("path") == file_path:
                return movie
        return None

    def get_movie(self, movie_id):
        return self._get(f"/movie/{movie_id}")

    def get_movie_language(self, movie_id):
        movie = self.get_movie(movie_id)
        lang = movie.get("originalLanguage", {})
        return lang.get("name", "").lower()
