import os


class Config:
    BAZARR_URL = os.environ.get("BAZARR_URL", "http://bazarr:6767")
    BAZARR_API_KEY = os.environ.get("BAZARR_API_KEY", "")
    RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
    RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
    SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
    SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
    WHISPER_URL = os.environ.get("WHISPER_URL", "")
    WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")
    WEBHOOK_SECRET = os.environ.get("TALKARR_WEBHOOK_SECRET", "")
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/talkarr.db")
    BASE_URL = os.environ.get("BASE_URL", "http://talkarr:9494")
    MEDIA_PATH = os.environ.get("MEDIA_PATH", "/data")
    SYNC_ENABLED = os.environ.get("SYNC_ENABLED", "true").lower() == "true"
    CLEAN_ENABLED = os.environ.get("CLEAN_ENABLED", "true").lower() == "true"
    GENERATE_ENABLED = os.environ.get("GENERATE_ENABLED", "false").lower() == "true"
    SYNC_SCORE_THRESHOLD = float(os.environ.get("SYNC_SCORE_THRESHOLD", "0.4"))
    PATH_MAPPING = os.environ.get("PATH_MAPPING", "")
