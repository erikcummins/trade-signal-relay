import os
import threading

import pytest

from relay_publisher.publisher import SignalPublisher
from relay_client.client import RelayClient


RELAY_WS_URL = os.environ.get("RELAY_WS_URL")
PUBLISHER_KEY = os.environ.get("RELAY_PUBLISHER_KEY", "pub_test_abc123")
SUBSCRIBER_KEY = os.environ.get("RELAY_SUBSCRIBER_KEY", "sub_test_abc123")

skip_no_aws = pytest.mark.skipif(
    not RELAY_WS_URL, reason="RELAY_WS_URL not set"
)


@skip_no_aws
class TestAWSSmoke:
    def test_publisher_connect_and_send(self):
        publisher = SignalPublisher(RELAY_WS_URL, PUBLISHER_KEY)
        publisher.connect()
        publisher.publish_open("AAPL", "buy", 2.0, 1.0)
        publisher.disconnect()

    def test_subscriber_receives_signal(self):
        received = []
        received_event = threading.Event()

        def on_signal(signal):
            received.append(signal)
            received_event.set()

        client = RelayClient(RELAY_WS_URL, SUBSCRIBER_KEY, on_signal)
        client.connect()

        publisher = SignalPublisher(RELAY_WS_URL, PUBLISHER_KEY)
        publisher.connect()
        publisher.publish_open("TSLA", "sell", 3.0, 1.5)

        received_event.wait(timeout=10)

        publisher.disconnect()
        client.disconnect()

        assert len(received) == 1
        assert received[0].ticker == "TSLA"
