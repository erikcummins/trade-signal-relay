import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import yaml

from relay_client.config import load_config, ConfigError
from relay_client.trader import AlpacaTrader
from relay_client.position_manager import PositionManager
from shared.messages import Signal


VALID_CONFIG = {
    "relay_server": "wss://example.com",
    "access_key": "sub_user1_abc",
    "alpaca": {
        "api_key": "ak_test",
        "secret_key": "sk_test",
        "paper": True,
    },
    "trading": {"position_size": 5000},
    "eod": {"stop_new_positions_minutes": 15, "close_all_minutes": 5},
    "discord": {"bot_token": "token123", "channel_id": "chan456"},
}


def _write_config(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(data, f)
    f.close()
    return f.name


def _make_signal(side="buy", ticker="AAPL", tp=2.0, sl=1.0) -> Signal:
    return Signal(
        signal_id="sig1", action="open", ticker=ticker,
        side=side, tp_percent=tp, sl_percent=sl,
        timestamp="2026-03-12T10:00:00Z", algo_id="algo1",
    )


class TestConfigLoading:
    def test_valid_config(self):
        path = _write_config(VALID_CONFIG)
        try:
            cfg = load_config(path)
            assert cfg.relay_server == "wss://example.com"
            assert cfg.access_key == "sub_user1_abc"
            assert cfg.alpaca.api_key == "ak_test"
            assert cfg.alpaca.secret_key == "sk_test"
            assert cfg.alpaca.paper is True
            assert cfg.trading.position_size == 5000
            assert cfg.eod.stop_new_positions_minutes == 15
            assert cfg.eod.close_all_minutes == 5
            assert cfg.discord.bot_token == "token123"
            assert cfg.discord.channel_id == "chan456"
        finally:
            os.unlink(path)

    def test_defaults(self):
        minimal = {
            "relay_server": "wss://example.com",
            "access_key": "sub_user1_abc",
            "alpaca": {"api_key": "ak", "secret_key": "sk"},
        }
        path = _write_config(minimal)
        try:
            cfg = load_config(path)
            assert cfg.alpaca.paper is False
            assert cfg.trading.position_size == 10000
            assert cfg.eod.stop_new_positions_minutes == 20
            assert cfg.eod.close_all_minutes == 10
            assert cfg.discord.bot_token is None
            assert cfg.discord.channel_id is None
        finally:
            os.unlink(path)

    @pytest.mark.parametrize("missing_key", ["relay_server", "access_key"])
    def test_missing_top_level(self, missing_key):
        data = {**VALID_CONFIG}
        del data[missing_key]
        path = _write_config(data)
        try:
            with pytest.raises(ConfigError, match=missing_key):
                load_config(path)
        finally:
            os.unlink(path)

    @pytest.mark.parametrize("missing_key", ["api_key", "secret_key"])
    def test_missing_alpaca_field(self, missing_key):
        data = {**VALID_CONFIG, "alpaca": {**VALID_CONFIG["alpaca"]}}
        del data["alpaca"][missing_key]
        path = _write_config(data)
        try:
            with pytest.raises(ConfigError, match=missing_key):
                load_config(path)
        finally:
            os.unlink(path)


class TestAlpacaTrader:
    @patch("relay_client.trader.tradeapi")
    def _make_trader(self, mock_tradeapi):
        mock_api = MagicMock()
        mock_tradeapi.REST.return_value = mock_api
        trader = AlpacaTrader("ak", "sk", paper=True, position_size=10000)
        return trader, mock_api

    def test_buy_tp_sl(self):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)

        result = trader.execute_signal(_make_signal(side="buy", tp=2.0, sl=1.0))

        assert result["tp_price"] == 102.0
        assert result["sl_price"] == 99.0
        assert result["side"] == "buy"

    def test_sell_tp_sl(self):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=200.0)

        result = trader.execute_signal(_make_signal(side="sell", tp=5.0, sl=2.0))

        assert result["tp_price"] == 190.0
        assert result["sl_price"] == 204.0
        assert result["side"] == "sell"

    def test_share_calculation(self):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=33.33)

        result = trader.execute_signal(_make_signal())

        assert result["shares"] == int(10000 / 33.33)

    def test_duplicate_position_skipped(self):
        trader, mock_api = self._make_trader()
        mock_api.get_position.return_value = MagicMock()

        result = trader.execute_signal(_make_signal())

        assert result is None
        mock_api.submit_order.assert_not_called()

    def test_bracket_order_params(self):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=50.0)

        trader.execute_signal(_make_signal(side="buy", tp=4.0, sl=2.0))

        mock_api.submit_order.assert_called_once_with(
            symbol="AAPL",
            qty=200,
            side="buy",
            type="market",
            time_in_force="day",
            order_class="bracket",
            stop_loss={"stop_price": 49.0},
            take_profit={"limit_price": 52.0},
        )


class TestPositionManager:
    def _make_clock(self, is_open, minutes_to_close):
        clock = MagicMock()
        clock.is_open = is_open
        now = datetime(2026, 3, 12, 15, 0, 0)
        clock.timestamp = now
        clock.next_close = now + timedelta(minutes=minutes_to_close)
        return clock

    def test_market_closed(self):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=False, minutes_to_close=60)
        pm = PositionManager(api)

        assert pm.check_market_hours() is False

    def test_accepting_positions_when_far_from_close(self):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=60)
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        assert pm.check_market_hours() is True
        assert pm.accepting_new_positions is True

    def test_stop_new_at_threshold(self):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=15)
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        pm.check_market_hours()

        assert pm.accepting_new_positions is False
        assert pm.positions_closed_for_day is False

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_at_threshold(self, mock_sleep):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=5)
        api.list_positions.return_value = []
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        pm.check_market_hours()

        assert pm.accepting_new_positions is False
        assert pm.positions_closed_for_day is True
        api.cancel_all_orders.assert_called_once()
        api.close_all_positions.assert_called_once()

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_retry(self, mock_sleep):
        api = MagicMock()
        api.list_positions.side_effect = [
            [MagicMock()],
            [MagicMock()],
            [],
        ]
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        pm.close_all_positions()

        assert mock_sleep.call_count == 3
        assert api.list_positions.call_count == 3

    def test_reset(self):
        api = MagicMock()
        pm = PositionManager(api)
        pm.accepting_new_positions = False
        pm.positions_closed_for_day = True
        pm.market_close_time = datetime.now()

        pm.reset()

        assert pm.accepting_new_positions is True
        assert pm.positions_closed_for_day is False
        assert pm.market_close_time is None
