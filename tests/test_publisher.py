import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from relay_publisher.publisher import SignalPublisher
from shared.messages import AuthResult, Signal, serialize, deserialize


class TestKeyValidation:
    def test_valid_key_accepted(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        assert pub._publisher_key == "pub_algo1_abc123"

    def test_invalid_key_raises(self):
        with pytest.raises(ValueError, match="Invalid publisher key"):
            SignalPublisher("ws://localhost:8000", "bad_key")

    def test_subscriber_key_raises(self):
        with pytest.raises(ValueError, match="Invalid publisher key"):
            SignalPublisher("ws://localhost:8000", "sub_user1_abc123")


class TestMessageConstruction:
    def test_publish_open_creates_signal(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub.publish_open("AAPL", "buy", 2.5, 1.0)

        msg = pub._queue.get_nowait()
        assert isinstance(msg, Signal)
        assert msg.action == "open"
        assert msg.ticker == "AAPL"
        assert msg.side == "buy"
        assert msg.tp_percent == 2.5
        assert msg.sl_percent == 1.0

    def test_publish_open_has_uuid(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub.publish_open("TSLA", "sell", 3.0, 1.5)

        msg = pub._queue.get_nowait()
        uuid.UUID(msg.signal_id)

    def test_publish_open_has_timestamp(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub.publish_open("GOOG", "buy", 1.0, 0.5)

        msg = pub._queue.get_nowait()
        assert "T" in msg.timestamp
        assert msg.timestamp.endswith("+00:00")

    def test_publish_open_serializes_correctly(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub.publish_open("AAPL", "buy", 2.5, 1.0)

        msg = pub._queue.get_nowait()
        data = json.loads(serialize(msg))
        assert data["type"] == "signal"
        assert data["action"] == "open"
        assert data["ticker"] == "AAPL"


class TestAuthFlow:
    @pytest.mark.asyncio
    async def test_connect_sends_auth_and_waits(self):
        auth_result = serialize(AuthResult(success=True))

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        await pub._authenticate(mock_ws)

        sent_data = mock_ws.send.call_args[0][0]
        parsed = deserialize(sent_data)
        assert parsed.publisher_key == "pub_algo1_abc123"
        assert parsed.type == "auth"

    @pytest.mark.asyncio
    async def test_auth_failure_raises(self):
        auth_result = serialize(AuthResult(success=False))

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")

        with pytest.raises(ConnectionError, match="Authentication failed"):
            await pub._authenticate(mock_ws)


def _make_failing_connect_cm():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=OSError("Connection refused"))
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestReconnect:
    @pytest.mark.asyncio
    async def test_backoff_doubles_on_failure(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        call_count = 0
        sleep_times = []

        def fake_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                pub._stop.set()
            return _make_failing_connect_cm()

        async def fake_sleep(t):
            sleep_times.append(t)

        with patch("relay_publisher.publisher.websockets.connect", side_effect=fake_connect), \
             patch("relay_publisher.publisher.asyncio.sleep", side_effect=fake_sleep):
            await pub._connection_loop()

        assert sleep_times == [1, 2, 4]

    @pytest.mark.asyncio
    async def test_backoff_caps_at_30(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub._backoff = 16
        call_count = 0
        sleep_times = []

        def fake_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                pub._stop.set()
            return _make_failing_connect_cm()

        async def fake_sleep(t):
            sleep_times.append(t)

        with patch("relay_publisher.publisher.websockets.connect", side_effect=fake_connect), \
             patch("relay_publisher.publisher.asyncio.sleep", side_effect=fake_sleep):
            await pub._connection_loop()

        assert sleep_times == [16, 30]

    @pytest.mark.asyncio
    async def test_backoff_resets_on_success(self):
        pub = SignalPublisher("ws://localhost:8000", "pub_algo1_abc123")
        pub._backoff = 16
        auth_result = serialize(AuthResult(success=True))

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        async def enter_ws(*a, **k):
            pub._queue.put(None)
            return mock_ws

        async def exit_ws(*a, **k):
            pub._stop.set()
            return False

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=enter_ws)
        mock_cm.__aexit__ = AsyncMock(side_effect=exit_ws)

        with patch("relay_publisher.publisher.websockets.connect", return_value=mock_cm):
            await pub._connection_loop()

        assert pub._backoff == 1
