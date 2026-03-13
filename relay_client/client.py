import asyncio
import threading

import websockets

from shared.messages import AuthSubscriber, AuthResult, Signal, serialize, deserialize


class RelayClient:
    def __init__(self, server_url: str, subscriber_key: str, on_signal_callback):
        self._server_url = server_url
        self._subscriber_key = subscriber_key
        self._on_signal = on_signal_callback
        self._last_signal_id = None
        self._thread = None
        self._loop = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._backoff = 1

    def connect(self):
        self._stop.clear()
        self._connected.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._connected.wait()

    def disconnect(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        finally:
            self._loop.close()

    async def _connection_loop(self):
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._server_url) as ws:
                    self._backoff = 1
                    await self._authenticate(ws)
                    self._connected.set()
                    await self._receive_loop(ws)
            except (OSError, websockets.exceptions.WebSocketException):
                if self._stop.is_set():
                    break
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30)

    async def _authenticate(self, ws):
        auth_msg = AuthSubscriber(
            subscriber_key=self._subscriber_key,
            last_signal_id=self._last_signal_id,
        )
        await ws.send(serialize(auth_msg))
        response = await ws.recv()
        result = deserialize(response)
        if not isinstance(result, AuthResult) or not result.success:
            raise ConnectionError("Authentication failed")

    async def _receive_loop(self, ws):
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            msg = deserialize(raw)
            if isinstance(msg, Signal):
                self._last_signal_id = msg.signal_id
                self._on_signal(msg)
