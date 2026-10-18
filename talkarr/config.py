import os


class Config:
    BAZARR_URL = os.environ.get("BAZARR_URL", "http://bazarr:6767")
    BAZARR_API_KEY = os.environ.get("BAZARR_API_KEY", "")
    # When Talkarr removes a subtitle Bazarr downloaded, blacklist it in Bazarr (Bazarr
    # deletes it and searches for a different one) instead of only deleting the file,
    # which would let Bazarr fetch the same subtitle again. Needs BAZARR_API_KEY.
    BAZARR_BLACKLIST = os.environ.get("BAZARR_BLACKLIST", "true").lower() == "true"
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
    MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
    SYNC_ENABLED = os.environ.get("SYNC_ENABLED", "true").lower() == "true"
    CLEAN_ENABLED = os.environ.get("CLEAN_ENABLED", "true").lower() == "true"
    # Text modifications, with Bazarr's names (its "Subtitle modifications"), e.g.
    # "common,remove_tags,OCR_fixes,remove_HI,fix_uppercase,reverse_rtl,emoji,color(name=yellow)".
    # Empty turns them off. See talkarr/mods.py.
    SUBTITLE_MODS = os.environ.get("SUBTITLE_MODS", "")
    SYNC_SCORE_THRESHOLD = float(os.environ.get("SYNC_SCORE_THRESHOLD", "0.4"))
    PATH_MAPPING = os.environ.get("PATH_MAPPING", "")
    # Operating mode at first start: manual | safe_auto | full_auto. The settings page can
    # change it at runtime (stored in the database); this is only the default.
    MODE = os.environ.get("TALKARR_MODE", "manual").lower()
    # What applying a "mislabelled forced subtitle" fix does:
    #   remove                      - delete it, no backup (can't be restored)
    #   remove_and_backup           - delete it after copying it to BACKUP_DIR
    #   rename_or_remove_and_backup - make it the regular subtitle if there is none, else
    #                                 delete it; backed up either way
    #   flag                        - never touch it; only report it
    # Removals of subtitles Bazarr downloaded go through Bazarr's blacklist (BAZARR_BLACKLIST).
    # ("remove" and "rename_or_remove" are accepted as older names.)
    MISLABELLED_ACTION = os.environ.get("MISLABELLED_ACTION", "rename_or_remove_and_backup").lower()
    BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")
    # Regular/HI syncs are proposed when ffsubsync moves a subtitle at least this much,
    # and count as confident (applied in Full Auto) at or above this confidence.
    SYNC_MIN_OFFSET = float(os.environ.get("SYNC_MIN_OFFSET", "0.15"))
    SYNC_MIN_CONFIDENCE = float(os.environ.get("SYNC_MIN_CONFIDENCE", "0.8"))
    FORCED_VERIFY_ENABLED = os.environ.get("FORCED_VERIFY_ENABLED", "true").lower() == "true"
    ML_URL = os.environ.get("TALKARR_ML_URL", "")
    CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
