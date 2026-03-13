import json
import urllib.request


class WebhookNotifier:
    def __init__(self, webhook_url: str):
        self._url = webhook_url

    def send_message(self, text: str):
        try:
            data = json.dumps({"content": text[:2000]}).encode()
            req = urllib.request.Request(self._url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def shutdown(self):
        pass


class NoOpNotifier:
    def send_message(self, text: str):
        pass

    def shutdown(self):
        pass


def create_notifier(discord_config):
    if discord_config and discord_config.webhook_url:
        return WebhookNotifier(discord_config.webhook_url)
    return NoOpNotifier()
