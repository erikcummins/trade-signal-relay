from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import ProtocolError

from relay_client.discord_bot import NoOpNotifier, WebhookNotifier, create_notifier
from relay_client.client import RelayClient
from shared.messages import AuthResult, Signal, serialize, deserialize


def _make_signal(signal_id="sig1"):
    return Signal(
        signal_id=signal_id, action="open", ticker="AAPL",
        side="buy", tp_percent=2.0, sl_percent=1.0,
        timestamp="2026-03-12T10:00:00Z", algo_id="algo1",
    )


class TestNoOpNotifier:
    def test_send_message_does_nothing(self):
        notifier = NoOpNotifier()
        notifier.send_message("test")

    def test_shutdown_does_nothing(self):
        notifier = NoOpNotifier()
        notifier.shutdown()

    def test_create_notifier_without_config(self):
        result = create_notifier(None)
        assert isinstance(result, NoOpNotifier)

    def test_create_notifier_with_empty_config(self):
        config = MagicMock()
        config.webhook_url = None
        result = create_notifier(config)
        assert isinstance(result, NoOpNotifier)


class TestWebhookNotifier:
    def test_create_notifier_with_webhook(self):
        config = MagicMock()
        config.webhook_url = "https://discord.com/api/webhooks/123/abc"
        result = create_notifier(config)
        assert isinstance(result, WebhookNotifier)

    @patch("relay_client.discord_bot.urllib.request.urlopen")
    def test_send_message_posts_to_webhook(self, mock_urlopen):
        notifier = WebhookNotifier("https://discord.com/api/webhooks/123/abc")
        notifier.send_message("hello")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://discord.com/api/webhooks/123/abc"
        assert req.method == "POST"
        import json
        body = json.loads(req.data.decode())
        assert body["content"] == "hello"

    @patch("relay_client.discord_bot.urllib.request.urlopen", side_effect=Exception("fail"))
    def test_send_message_swallows_errors(self, mock_urlopen):
        notifier = WebhookNotifier("https://bad-url")
        notifier.send_message("hello")


class TestRelayClientAuth:
    @pytest.mark.asyncio
    async def test_sends_auth_and_receives_result(self):
        auth_result = serialize(AuthResult(success=True))
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        client = RelayClient("ws://localhost", "sub_user1_abc", lambda s: None)
        await client._authenticate(mock_ws)

        sent_data = mock_ws.send.call_args[0][0]
        parsed = deserialize(sent_data)
        assert parsed.subscriber_key == "sub_user1_abc"
        assert parsed.type == "auth"

    @pytest.mark.asyncio
    async def test_auth_failure_raises(self):
        auth_result = serialize(AuthResult(success=False))
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        client = RelayClient("ws://localhost", "sub_user1_abc", lambda s: None)

        with pytest.raises(ConnectionError, match="Authentication failed"):
            await client._authenticate(mock_ws)


class TestRelayClientSignalDispatch:
    @pytest.mark.asyncio
    async def test_signal_calls_callback(self):
        signal = _make_signal()
        received = []

        client = RelayClient("ws://localhost", "sub_user1_abc", received.append)

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[serialize(signal), AsyncMock(side_effect=StopIteration)]
        )

        recv_count = 0

        async def recv_side_effect():
            nonlocal recv_count
            recv_count += 1
            if recv_count == 1:
                return serialize(signal)
            client._stop.set()
            raise asyncio.TimeoutError()

        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)

        import asyncio
        await client._receive_loop(mock_ws)

        assert len(received) == 1
        assert received[0].signal_id == "sig1"
        assert received[0].ticker == "AAPL"

    @pytest.mark.asyncio
    async def test_updates_last_signal_id(self):
        client = RelayClient("ws://localhost", "sub_user1_abc", lambda s: None)
        assert client._last_signal_id is None

        recv_count = 0

        async def recv_side_effect():
            nonlocal recv_count
            recv_count += 1
            if recv_count == 1:
                return serialize(_make_signal("sig_aaa"))
            if recv_count == 2:
                return serialize(_make_signal("sig_bbb"))
            client._stop.set()
            raise asyncio.TimeoutError()

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)

        import asyncio
        await client._receive_loop(mock_ws)

        assert client._last_signal_id == "sig_bbb"


class TestRelayClientReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_sends_last_signal_id(self):
        client = RelayClient("ws://localhost", "sub_user1_abc", lambda s: None)
        client._last_signal_id = "sig_prev"

        auth_result = serialize(AuthResult(success=True))
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=auth_result)
        mock_ws.send = AsyncMock()

        await client._authenticate(mock_ws)

        sent_data = mock_ws.send.call_args[0][0]
        parsed = deserialize(sent_data)
        assert parsed.last_signal_id == "sig_prev"

    @pytest.mark.asyncio
    async def test_backoff_doubles_on_failure(self):
        client = RelayClient("ws://localhost", "sub_user1_abc", lambda s: None)
        call_count = 0
        sleep_times = []

        def fake_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                client._stop.set()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=OSError("refused"))
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        async def fake_sleep(t):
            sleep_times.append(t)

        with patch("relay_client.client.websockets.connect", side_effect=fake_connect), \
             patch("relay_client.client.asyncio.sleep", side_effect=fake_sleep):
            await client._connection_loop()

        assert sleep_times == [1, 2, 4]


class TestMainLoop:
    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_market_open_sleeps_5(self, mock_load, mock_trader_cls, mock_tradeapi,
                                  mock_pm_cls, mock_notifier_fn, mock_client_cls,
                                  mock_sleep):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        pm.market_close_time = None
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt()
            return True

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        mock_sleep.assert_called_with(5)

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_market_open_notifies_close_time(self, mock_load, mock_trader_cls,
                                              mock_tradeapi, mock_pm_cls,
                                              mock_notifier_fn, mock_client_cls,
                                              mock_sleep):
        from datetime import datetime

        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        pm.market_close_time = datetime(2026, 3, 12, 16, 0, 0)
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt()
            return True

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        mock_notifier.send_message.assert_any_call("Market open (closes 04:00 PM)")

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_market_closed_resets_and_sleeps_60(self, mock_load, mock_trader_cls,
                                                mock_tradeapi, mock_pm_cls,
                                                mock_notifier_fn, mock_client_cls,
                                                mock_sleep):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True
            if call_count == 2:
                return False
            raise KeyboardInterrupt()

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        pm.reset.assert_called_once()
        mock_sleep.assert_any_call(60)
        mock_notifier.send_message.assert_any_call("Market closed")

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_api_error_retries(self, mock_load, mock_trader_cls, mock_tradeapi,
                                mock_pm_cls, mock_notifier_fn, mock_client_cls,
                                mock_sleep):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RequestsConnectionError("Remote end closed connection")
            if call_count == 2:
                return True
            raise KeyboardInterrupt()

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        pm.market_close_time = None
        mock_pm_cls.return_value = pm

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        assert call_count == 3
        mock_sleep.assert_any_call(10)

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_api_error_resets_connections_after_consecutive_failures(
        self, mock_load, mock_trader_cls, mock_tradeapi, mock_pm_cls,
        mock_notifier_fn, mock_client_cls, mock_sleep,
    ):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        pm.market_close_time = None
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                raise RequestsConnectionError("Connection aborted")
            if call_count == 6:
                return True
            raise KeyboardInterrupt()

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_trader = MagicMock()
        mock_trader_cls.return_value = mock_trader

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        assert mock_tradeapi.REST.call_count == 2
        mock_trader.reset_connection.assert_called_once()
        notify_calls = [c[0][0] for c in mock_notifier.send_message.call_args_list]
        assert any("API connections reset" in msg for msg in notify_calls)

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_api_backoff_resets_on_success(self, mock_load, mock_trader_cls,
                                           mock_tradeapi, mock_pm_cls,
                                           mock_notifier_fn, mock_client_cls,
                                           mock_sleep):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        pm.market_close_time = None
        call_count = 0

        def check_hours():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RequestsConnectionError("Connection aborted")
            if call_count == 3:
                return True
            if call_count == 4:
                raise RequestsConnectionError("Connection aborted")
            raise KeyboardInterrupt()

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert sleep_calls[0] == 10
        assert sleep_calls[1] == 20
        assert sleep_calls[2] == 5
        assert sleep_calls[3] == 10

    @patch("relay_client.__main__.time.sleep")
    @patch("relay_client.__main__.RelayClient")
    @patch("relay_client.__main__.create_notifier")
    @patch("relay_client.__main__.PositionManager")
    @patch("relay_client.__main__.tradeapi")
    @patch("relay_client.__main__.AlpacaTrader")
    @patch("relay_client.__main__.load_config")
    def test_signal_api_error_does_not_crash(self, mock_load, mock_trader_cls,
                                              mock_tradeapi, mock_pm_cls,
                                              mock_notifier_fn, mock_client_cls,
                                              mock_sleep):
        config = MagicMock()
        config.alpaca.paper = True
        mock_load.return_value = config

        pm = MagicMock()
        pm.accepting_new_positions = True

        def check_hours():
            raise KeyboardInterrupt()

        pm.check_market_hours = MagicMock(side_effect=check_hours)
        mock_pm_cls.return_value = pm

        mock_trader = MagicMock()
        mock_trader.execute_signal.side_effect = ProtocolError("Connection aborted")
        mock_trader_cls.return_value = mock_trader

        mock_notifier = MagicMock()
        mock_notifier_fn.return_value = mock_notifier

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        from relay_client.__main__ import main

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=MagicMock(config="test.yaml")):
            main()

        # Grab the on_signal callback and invoke it with a failing trader
        on_signal = mock_client_cls.call_args[0][2]
        signal = Signal(
            signal_id="sig1", action="open", ticker="AAPL",
            side="buy", tp_percent=2.0, sl_percent=1.0,
            timestamp="2026-03-12T10:00:00Z", algo_id="algo1",
        )
        on_signal(signal)

        # Verify it notified about the failure instead of crashing
        notify_calls = [c[0][0] for c in mock_notifier.send_message.call_args_list]
        assert any("Failed to execute signal AAPL" in msg for msg in notify_calls)
