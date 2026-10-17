import json

import requests
import responses

from talkarr.config import Config
from talkarr.notify import send_discord_notification

HOOK = "https://discord.example/api/webhooks/1/x"


@responses.activate
def test_no_url_sends_nothing():
    send_discord_notification("hello")
    assert len(responses.calls) == 0


@responses.activate
def test_posts_content(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_WEBHOOK_URL", HOOK)
    responses.post(HOOK, status=204)

    send_discord_notification("hello")

    assert json.loads(responses.calls[0].request.body) == {"content": "hello"}


@responses.activate
def test_network_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(Config, "DISCORD_WEBHOOK_URL", HOOK)
    responses.post(HOOK, body=requests.ConnectionError("down"))

    send_discord_notification("hello")  # must not raise
