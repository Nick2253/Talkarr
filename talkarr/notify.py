import logging

import requests

from .config import Config

log = logging.getLogger(__name__)


def send_discord_notification(message):
    url = Config.DISCORD_WEBHOOK_URL
    if not url:
        return

    try:
        requests.post(url, json={"content": message}, timeout=10)
    except Exception:
        log.exception("Failed to send Discord notification")
