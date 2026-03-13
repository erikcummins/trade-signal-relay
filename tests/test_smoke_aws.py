import asyncio
import os
import threading
import time

import pytest
import websockets

from relay_publisher.publisher import SignalPublisher
from relay_client.client import RelayClient
from shared.messages import AuthSubscriber, AuthResult, serialize, deserialize


RELAY_WS_URL = os.environ.get("RELAY_WS_URL")
PUBLISHER_KEY = os.environ.get("RELAY_PUBLISHER_KEY", "pub_test_abc123")
SUBSCRIBER_KEY = os.environ.get("RELAY_SUBSCRIBER_KEY", "sub_test_abc123")
PUBLISHER_KEY_2 = os.environ.get("RELAY_PUBLISHER_KEY_2")

skip_no_aws = pytest.mark.skipif(
    not RELAY_WS_URL, reason="RELAY_WS_URL not set"
)

skip_no_key2 = pytest.mark.skipif(
    not PUBLISHER_KEY_2, reason="RELAY_PUBLISHER_KEY_2 not set"
)


def _connect_publisher(key=None):
    pub = SignalPublisher(RELAY_WS_URL, key or PUBLISHER_KEY)
    pub.connect()
    return pub


def _connect_subscriber(signal_count=1):
    received = []
    event = threading.Event()

    def on_signal(signal):
        received.append(signal)
        if len(received) >= signal_count:
            event.set()

    client = RelayClient(RELAY_WS_URL, SUBSCRIBER_KEY, on_signal)
    client.connect()
    return client, received, event


@skip_no_aws
class TestAWSSmoke:
    def test_publisher_connect_and_send(self):
        publisher = _connect_publisher()
        publisher.publish_open("AAPL", "buy", 2.0, 1.0)
        publisher.disconnect()

    def test_subscriber_receives_signal(self):
        client, received, event = _connect_subscriber()
        publisher = _connect_publisher()
        publisher.publish_open("TSLA", "sell", 3.0, 1.5)
        event.wait(timeout=10)
        publisher.disconnect()
        client.disconnect()
        assert len(received) == 1
        assert received[0].ticker == "TSLA"

    def test_auth_rejection(self):
        async def _test():
            async with websockets.connect(RELAY_WS_URL) as ws:
                auth_msg = AuthSubscriber(subscriber_key="sub_fake_invalidkey")
                await ws.send(serialize(auth_msg))
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=5)
                    result = deserialize(response)
                    assert not (isinstance(result, AuthResult) and result.success)
                except websockets.exceptions.ConnectionClosed:
                    pass

        asyncio.run(_test())

    def test_signal_field_integrity(self):
        client, received, event = _connect_subscriber()
        publisher = _connect_publisher()
        publisher.publish_open("GOOG", "buy", 4.5, 2.5)
        event.wait(timeout=10)
        publisher.disconnect()
        client.disconnect()

        assert len(received) == 1
        sig = received[0]
        assert sig.ticker == "GOOG"
        assert sig.side == "buy"
        assert sig.action == "open"
        assert sig.tp_percent == 4.5
        assert sig.sl_percent == 2.5
        assert sig.algo_id is not None
        assert sig.signal_id is not None
        assert sig.timestamp is not None

    def test_multiple_signals(self):
        client, received, event = _connect_subscriber(signal_count=3)
        publisher = _connect_publisher()

        tickers = ["AAPL", "GOOG", "MSFT"]
        for ticker in tickers:
            publisher.publish_open(ticker, "buy", 1.0, 1.0)
            time.sleep(0.2)

        event.wait(timeout=15)
        publisher.disconnect()
        client.disconnect()

        assert len(received) == 3
        assert {s.ticker for s in received} == set(tickers)

    @skip_no_key2
    def test_signal_routing_isolation(self):
        client, received, event = _connect_subscriber()

        publisher2 = _connect_publisher(key=PUBLISHER_KEY_2)
        publisher2.publish_open("MSFT", "buy", 1.0, 1.0)

        arrived = event.wait(timeout=3)

        publisher2.disconnect()
        client.disconnect()

        assert not arrived
        assert len(received) == 0

    def test_latency(self):
        received_times = []
        event = threading.Event()

        def on_signal(signal):
            received_times.append(time.monotonic())
            event.set()

        client = RelayClient(RELAY_WS_URL, SUBSCRIBER_KEY, on_signal)
        client.connect()
        publisher = _connect_publisher()

        t0 = time.monotonic()
        publisher.publish_open("LATENCY", "buy", 1.0, 1.0)
        event.wait(timeout=10)

        publisher.disconnect()
        client.disconnect()

        assert len(received_times) == 1
        latency = received_times[0] - t0
        print(f"\nRound-trip latency: {latency:.3f}s")
        assert latency < 2.0
