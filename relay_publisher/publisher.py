import asyncio
import queue
import threading
import uuid
from datetime import datetime, timezone

import websockets

from shared.auth import validate_publisher_key
from shared.messages import AuthPublisher, AuthResult, Signal, serialize, deserialize


class SignalPublisher:
    def __init__(self, server_url: str, publisher_key: str):
        if not validate_publisher_key(publisher_key):
            raise ValueError(f"Invalid publisher key: {publisher_key}")
        self._server_url = server_url
        self._publisher_key = publisher_key
        self._queue = queue.Queue()
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

    def publish_open(self, ticker: str, side: str, tp_percent: float, sl_percent: float):
        signal = Signal(
            signal_id=str(uuid.uuid4()),
            action="open",
            ticker=ticker,
            side=side,
            tp_percent=tp_percent,
            sl_percent=sl_percent,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._queue.put(signal)

    def disconnect(self):
        self._stop.set()
        self._queue.put(None)
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
                    await self._send_loop(ws)
            except (OSError, websockets.exceptions.WebSocketException):
                if self._stop.is_set():
                    break
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30)

    async def _authenticate(self, ws):
        auth_msg = AuthPublisher(publisher_key=self._publisher_key)
        await ws.send(serialize(auth_msg))
        response = await ws.recv()
        result = deserialize(response)
        if not isinstance(result, AuthResult) or not result.success:
            raise ConnectionError("Authentication failed")

    async def _send_loop(self, ws):
        while not self._stop.is_set():
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, self._queue.get, True, 0.1
                )
            except queue.Empty:
                continue
            if msg is None:
                break
            await ws.send(serialize(msg))
