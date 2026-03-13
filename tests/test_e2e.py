import asyncio
import threading

import pytest
import websockets

from shared.messages import AuthResult, Signal, serialize, deserialize
from shared.auth import validate_publisher_key, validate_subscriber_key
from relay_publisher.publisher import SignalPublisher
from relay_client.client import RelayClient


class LocalRelay:
    def __init__(self):
        self._publishers = {}
        self._subscribers = {}
        self._server = None
        self._port = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    @property
    def url(self):
        return f"ws://localhost:{self._port}"

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_server())
        self._ready.set()
        self._loop.run_forever()
        self._server.close()
        self._loop.run_until_complete(self._server.wait_closed())
        self._loop.close()

    async def _start_server(self):
        self._server = await websockets.serve(self._handle, "localhost", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, ws):
        try:
            raw = await ws.recv()
            msg = deserialize(raw)

            if hasattr(msg, "publisher_key"):
                if not validate_publisher_key(msg.publisher_key):
                    await ws.send(serialize(AuthResult(success=False)))
                    return
                await ws.send(serialize(AuthResult(success=True)))
                self._publishers[id(ws)] = ws
                try:
                    async for raw in ws:
                        parsed = deserialize(raw)
                        if isinstance(parsed, Signal):
                            for sub_ws in list(self._subscribers.values()):
                                await sub_ws.send(raw)
                finally:
                    self._publishers.pop(id(ws), None)

            elif hasattr(msg, "subscriber_key"):
                if not validate_subscriber_key(msg.subscriber_key):
                    await ws.send(serialize(AuthResult(success=False)))
                    return
                await ws.send(serialize(AuthResult(success=True)))
                self._subscribers[id(ws)] = ws
                try:
                    await ws.wait_closed()
                finally:
                    self._subscribers.pop(id(ws), None)
        except websockets.exceptions.ConnectionClosed:
            pass


class TestEndToEnd:
    def test_publisher_to_subscriber_signal_flow(self):
        relay = LocalRelay()
        relay.start()

        received = []
        received_event = threading.Event()

        def on_signal(signal):
            received.append(signal)
            received_event.set()

        try:
            publisher = SignalPublisher(relay.url, "pub_algo1_abc123")
            publisher.connect()

            client = RelayClient(relay.url, "sub_user1_xyz789", on_signal)
            client.connect()

            publisher.publish_open("AAPL", "buy", 2.5, 1.0)

            assert received_event.wait(timeout=5), "Timed out waiting for signal"

            assert len(received) == 1
            sig = received[0]
            assert sig.ticker == "AAPL"
            assert sig.side == "buy"
            assert sig.tp_percent == 2.5
            assert sig.sl_percent == 1.0
            assert sig.action == "open"
        finally:
            publisher.disconnect()
            client.disconnect()
            relay.stop()
