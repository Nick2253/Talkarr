"""Runtime settings and the read-only configuration view.

Everything except the operating mode comes from the environment (the compose file is
the source of truth). The mode starts from ``TALKARR_MODE`` and can be changed on the
settings page; the change is stored in the database.
"""

import os
from dataclasses import dataclass

from . import db
from .config import Config


class Mode:
    MANUAL = "manual"
    SAFE_AUTO = "safe_auto"
    FULL_AUTO = "full_auto"

    ALL = (MANUAL, SAFE_AUTO, FULL_AUTO)
    LABELS = {MANUAL: "Manual", SAFE_AUTO: "Safe Auto", FULL_AUTO: "Full Auto"}


MISLABELLED_ACTIONS = ("remove", "rename_or_remove", "flag")


def get_mode():
    stored = db.get_setting("mode")
    if stored in Mode.ALL:
        return stored
    return Config.MODE if Config.MODE in Mode.ALL else Mode.MANUAL


def set_mode(mode):
    if mode not in Mode.ALL:
        raise ValueError(f"unknown mode: {mode}")
    db.set_setting("mode", mode)


def mislabelled_action():
    action = Config.MISLABELLED_ACTION
    return action if action in MISLABELLED_ACTIONS else "flag"


@dataclass(frozen=True)
class Setting:
    group: str
    label: str
    env: str
    value: str
    from_env: bool  # set in the compose file, as opposed to Talkarr's default
    note: str = ""


def configuration():
    """Every environment-backed setting, for the read-only settings page."""

    def item(group, label, env, value, note="", secret=False):
        if secret:
            value = "set" if value else "not set"
        elif isinstance(value, bool):
            value = "on" if value else "off"
        return Setting(
            group, label, env, str(value) if value != "" else "not set", env in os.environ, note
        )

    return [
        item(
            "Library",
            "Media library",
            "MEDIA_PATH",
            Config.MEDIA_PATH,
            "Talkarr only reads and changes files inside this folder.",
        ),
        item(
            "Library",
            "Path mapping",
            "PATH_MAPPING",
            Config.PATH_MAPPING,
            "Translates paths Bazarr sends into this container's paths.",
        ),
        item(
            "Fixes",
            "Default mode",
            "TALKARR_MODE",
            Mode.LABELS.get(Config.MODE, Config.MODE),
            "Used until a mode is chosen on this page.",
        ),
        item(
            "Fixes",
            "Mislabelled forced subtitles",
            "MISLABELLED_ACTION",
            mislabelled_action().replace("_", " "),
        ),
        item(
            "Fixes",
            "Backups",
            "BACKUP_DIR",
            Config.BACKUP_DIR,
            "Originals are copied here before any change.",
        ),
        item(
            "Fixes", "Sync: smallest correction", "SYNC_MIN_OFFSET", f"{Config.SYNC_MIN_OFFSET} s"
        ),
        item(
            "Fixes",
            "Sync: confidence to auto-apply",
            "SYNC_MIN_CONFIDENCE",
            Config.SYNC_MIN_CONFIDENCE,
        ),
        item("Checks", "Clean subtitles", "CLEAN_ENABLED", Config.CLEAN_ENABLED),
        item("Checks", "Sync regular and HI subtitles", "SYNC_ENABLED", Config.SYNC_ENABLED),
        item(
            "Checks",
            "Verify forced subtitles",
            "FORCED_VERIFY_ENABLED",
            Config.FORCED_VERIFY_ENABLED,
        ),
        item(
            "Services",
            "Speech analysis (talkarr-ml)",
            "TALKARR_ML_URL",
            Config.ML_URL,
            "Empty turns off the audio check for forced subtitles.",
        ),
        item("Services", "Bazarr", "BAZARR_URL", Config.BAZARR_URL),
        item("Services", "Bazarr API key", "BAZARR_API_KEY", Config.BAZARR_API_KEY, secret=True),
        item(
            "Services",
            "Discord notifications",
            "DISCORD_WEBHOOK_URL",
            Config.DISCORD_WEBHOOK_URL,
            secret=True,
        ),
        item(
            "Services",
            "Webhook secret",
            "TALKARR_WEBHOOK_SECRET",
            Config.WEBHOOK_SECRET,
            "When set, Bazarr's webhook (/process) and /bulk must send it.",
            secret=True,
        ),
        item("Storage", "Database", "DATABASE_PATH", Config.DATABASE_PATH),
        item("Storage", "Cache", "CACHE_DIR", Config.CACHE_DIR),
    ]
