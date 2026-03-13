import asyncio
import threading

import discord


class SyncDiscordBot:
    def __init__(self, bot_token: str, channel_id: str):
        self._channel_id = int(channel_id)
        self._client = discord.Client(intents=discord.Intents.default())
        self._loop = None
        self._ready = threading.Event()

        @self._client.event
        async def on_ready():
            self._loop = asyncio.get_event_loop()
            self._ready.set()

        self._thread = threading.Thread(
            target=self._client.run, args=(bot_token,), daemon=True
        )
        self._thread.start()
        self._ready.wait()

    def send_message(self, text: str):
        channel = self._client.get_channel(self._channel_id)
        if channel:
            future = asyncio.run_coroutine_threadsafe(
                channel.send(text), self._loop
            )
            future.result(timeout=10)

    def shutdown(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)


class NoOpNotifier:
    def send_message(self, text: str):
        pass

    def shutdown(self):
        pass


def create_notifier(discord_config):
    if discord_config and discord_config.bot_token and discord_config.channel_id:
        return SyncDiscordBot(discord_config.bot_token, discord_config.channel_id)
    return NoOpNotifier()
