import os
import tempfile
from datetime import datetime, time as dt_time, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import yaml
from alpaca_trade_api.rest import APIError
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import ProtocolError

from relay_client.config import load_config, ConfigError, TradingConfig
from relay_client.trader import AlpacaTrader, FillError, ExitOrderError, MIN_SHORT_TP_PRICE
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
    "discord": {"webhook_url": "https://discord.com/api/webhooks/123/abc"},
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
            assert cfg.eod.eod_time is None
            assert cfg.discord.webhook_url == "https://discord.com/api/webhooks/123/abc"
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
            assert cfg.trading.algo_sizes is None
            assert cfg.eod.stop_new_positions_minutes == 20
            assert cfg.eod.close_all_minutes == 10
            assert cfg.discord.webhook_url is None
        finally:
            os.unlink(path)

    def test_algo_sizes(self):
        data = {**VALID_CONFIG, "trading": {
            "position_size": 10000,
            "algo_sizes": {"algo1": 5000, "algo2": 20000},
        }}
        path = _write_config(data)
        try:
            cfg = load_config(path)
            assert cfg.trading.algo_sizes == {"algo1": 5000, "algo2": 20000}
            assert cfg.trading.get_position_size("algo1") == 5000
            assert cfg.trading.get_position_size("algo2") == 20000
            assert cfg.trading.get_position_size("algo3") == 10000
            assert cfg.trading.get_position_size(None) == 10000
        finally:
            os.unlink(path)

    def test_eod_time_parsed(self):
        data = {**VALID_CONFIG, "eod": {**VALID_CONFIG["eod"], "eod_time": "13:30"}}
        path = _write_config(data)
        try:
            cfg = load_config(path)
            assert cfg.eod.eod_time == dt_time(13, 30)
        finally:
            os.unlink(path)

    def test_eod_time_invalid(self):
        data = {**VALID_CONFIG, "eod": {**VALID_CONFIG["eod"], "eod_time": "nope"}}
        path = _write_config(data)
        try:
            with pytest.raises(ConfigError, match="eod_time"):
                load_config(path)
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


def _setup_fill(mock_api, fill_price, filled_qty, *, statuses=("filled",)):
    entry = MagicMock(id="entry-1")
    mock_api.submit_order.side_effect = [entry, MagicMock(id="exit-1")]
    order_states = []
    for s in statuses:
        o = MagicMock()
        o.status = s
        o.filled_avg_price = str(fill_price)
        o.filled_qty = str(filled_qty)
        order_states.append(o)
    mock_api.get_order.side_effect = order_states


@patch("relay_client.trader.time.sleep")
class TestAlpacaTrader:
    @patch("relay_client.trader.tradeapi")
    def _make_trader(self, mock_tradeapi):
        mock_api = MagicMock()
        mock_tradeapi.REST.return_value = mock_api
        trader = AlpacaTrader("ak", "sk", paper=True, position_size=10000)
        return trader, mock_api

    def test_buy_tp_sl_uses_fill_price(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        _setup_fill(mock_api, fill_price=101.5, filled_qty=100)

        result = trader.execute_signal(_make_signal(side="buy", tp=2.0, sl=1.0))

        assert result["entry_price"] == 101.5
        assert result["tp_price"] == 103.53
        assert result["sl_price"] == round(101.5 * 0.99, 2)
        assert result["shares"] == 100

    def test_sell_tp_sl_uses_fill_price(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=200.0)
        _setup_fill(mock_api, fill_price=198.0, filled_qty=50)

        result = trader.execute_signal(_make_signal(side="sell", tp=5.0, sl=2.0))

        assert result["entry_price"] == 198.0
        assert result["tp_price"] == 188.10
        assert result["sl_price"] == 201.96

    def test_short_tp_clamped_above_zero(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=2.0)
        _setup_fill(mock_api, fill_price=2.0, filled_qty=5000)

        result = trader.execute_signal(_make_signal(side="sell", tp=99.5, sl=1.0))

        assert result["tp_price"] == MIN_SHORT_TP_PRICE
        assert result["tp_price"] > 0

    def test_share_calculation(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=33.33)
        _setup_fill(mock_api, fill_price=33.33, filled_qty=int(10000 / 33.33))

        result = trader.execute_signal(_make_signal())

        assert result["shares"] == int(10000 / 33.33)

    def test_position_size_override(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        _setup_fill(mock_api, fill_price=100.0, filled_qty=50)

        result = trader.execute_signal(_make_signal(), position_size=5000)

        assert result["shares"] == 50

    def test_duplicate_position_skipped(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.return_value = MagicMock()

        result = trader.execute_signal(_make_signal())

        assert result is None
        mock_api.submit_order.assert_not_called()

    @patch("relay_client.trader.tradeapi")
    def test_reset_connection(self, mock_tradeapi, _sleep):
        mock_api_1 = MagicMock()
        mock_api_2 = MagicMock()
        mock_tradeapi.REST.side_effect = [mock_api_1, mock_api_2]

        trader = AlpacaTrader("ak", "sk", paper=True, position_size=10000)
        assert trader.api is mock_api_1

        trader.reset_connection()
        assert trader.api is mock_api_2
        assert mock_tradeapi.REST.call_count == 2
        mock_tradeapi.REST.assert_called_with("ak", "sk", "https://paper-api.alpaca.markets")

    def test_market_then_oco_orders(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=50.0)
        _setup_fill(mock_api, fill_price=50.5, filled_qty=200)

        trader.execute_signal(_make_signal(side="buy", tp=4.0, sl=2.0))

        assert mock_api.submit_order.call_count == 2
        entry_kwargs = dict(mock_api.submit_order.call_args_list[0].kwargs)
        entry_cid = entry_kwargs.pop("client_order_id")
        assert entry_kwargs == {
            "symbol": "AAPL", "qty": 200, "side": "buy",
            "type": "market", "time_in_force": "day",
        }
        assert entry_cid.startswith("algo1-")
        exit_kwargs = dict(mock_api.submit_order.call_args_list[1].kwargs)
        exit_cid = exit_kwargs.pop("client_order_id")
        assert exit_kwargs == {
            "symbol": "AAPL", "qty": 200, "side": "sell",
            "type": "limit", "time_in_force": "day",
            "order_class": "oco",
            "take_profit": {"limit_price": 52.52},
            "stop_loss": {"stop_price": 49.49},
        }
        assert exit_cid.startswith("algo1-exit-")

    def test_oco_failure_kills_position(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        entry = MagicMock(id="entry-1")
        mock_api.submit_order.side_effect = [entry, Exception("oco rejected")]
        filled = MagicMock()
        filled.status = "filled"
        filled.filled_avg_price = "100.0"
        filled.filled_qty = "100"
        mock_api.get_order.return_value = filled

        with pytest.raises(ExitOrderError):
            trader.execute_signal(_make_signal(side="buy", tp=2.0, sl=1.0))

        mock_api.close_position.assert_called_once_with("AAPL")

    @patch("relay_client.trader.time.monotonic")
    def test_fill_timeout_cancels_order(self, mock_monotonic, _sleep):
        mock_monotonic.side_effect = [0.0, 0.1, 0.2, 999.0]
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        mock_api.submit_order.return_value = MagicMock(id="entry-1")
        pending = MagicMock()
        pending.status = "new"
        mock_api.get_order.return_value = pending

        with pytest.raises(FillError):
            trader.execute_signal(_make_signal())

        mock_api.cancel_order.assert_called_once_with("entry-1")

    def test_no_tp_uses_stop_only_exit(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        _setup_fill(mock_api, fill_price=100.0, filled_qty=100)

        sig = _make_signal(side="buy", tp=None, sl=1.0)
        result = trader.execute_signal(sig)

        assert result["tp_price"] is None
        assert result["sl_price"] == 99.0
        exit_kwargs = dict(mock_api.submit_order.call_args_list[1].kwargs)
        exit_kwargs.pop("client_order_id")
        assert exit_kwargs == {
            "symbol": "AAPL", "qty": 100, "side": "sell",
            "type": "stop", "time_in_force": "day",
            "stop_price": 99.0,
        }

    def test_no_tp_short_stop_only(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=50.0)
        _setup_fill(mock_api, fill_price=50.0, filled_qty=200)

        sig = _make_signal(side="sell", tp=None, sl=2.0)
        result = trader.execute_signal(sig)

        assert result["tp_price"] is None
        exit_call = mock_api.submit_order.call_args_list[1]
        assert exit_call.kwargs["type"] == "stop"
        assert exit_call.kwargs["side"] == "buy"
        assert exit_call.kwargs["stop_price"] == 51.0

    def test_no_tp_stop_failure_kills_position(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        entry = MagicMock(id="entry-1")
        mock_api.submit_order.side_effect = [entry, Exception("stop rejected")]
        filled = MagicMock()
        filled.status = "filled"
        filled.filled_avg_price = "100.0"
        filled.filled_qty = "100"
        mock_api.get_order.return_value = filled

        with pytest.raises(ExitOrderError):
            trader.execute_signal(_make_signal(tp=None, sl=1.0))

        mock_api.close_position.assert_called_once_with("AAPL")

    def test_client_order_id_uses_algo_prefix(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        _setup_fill(mock_api, fill_price=100.0, filled_qty=100)

        sig = Signal(
            signal_id="s1", action="open", ticker="AAPL", side="buy",
            tp_percent=2.0, sl_percent=1.0, timestamp="t", algo_id="newsmom",
        )
        trader.execute_signal(sig)

        entry_cid = mock_api.submit_order.call_args_list[0].kwargs["client_order_id"]
        exit_cid = mock_api.submit_order.call_args_list[1].kwargs["client_order_id"]
        assert entry_cid.startswith("newsmom-") and not entry_cid.startswith("newsmom-exit-")
        assert exit_cid.startswith("newsmom-exit-")
        assert entry_cid != exit_cid

    def test_no_algo_id_omits_client_order_id(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        _setup_fill(mock_api, fill_price=100.0, filled_qty=100)

        sig = Signal(
            signal_id="s1", action="open", ticker="AAPL", side="buy",
            tp_percent=2.0, sl_percent=1.0, timestamp="t", algo_id=None,
        )
        trader.execute_signal(sig)

        for call in mock_api.submit_order.call_args_list:
            assert "client_order_id" not in call.kwargs

    def test_rejected_order_raises(self, _sleep):
        trader, mock_api = self._make_trader()
        mock_api.get_position.side_effect = Exception("no position")
        mock_api.get_latest_trade.return_value = MagicMock(price=100.0)
        mock_api.submit_order.return_value = MagicMock(id="entry-1")
        rejected = MagicMock()
        rejected.status = "rejected"
        mock_api.get_order.return_value = rejected

        with pytest.raises(FillError):
            trader.execute_signal(_make_signal())

        assert mock_api.submit_order.call_count == 1


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

    def test_stop_new_notifies(self):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=15)
        notifier = MagicMock()
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, notifier=notifier)

        pm.check_market_hours()

        notifier.send_message.assert_called_once_with("Stopping new positions (15min to close)")

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_at_threshold(self, mock_sleep):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=5)
        api.list_orders.return_value = []
        api.list_positions.return_value = []
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        pm.check_market_hours()

        assert pm.accepting_new_positions is False
        assert pm.positions_closed_for_day is True
        api.cancel_all_orders.assert_called_once()
        api.close_all_positions.assert_called_once()

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_notifies(self, mock_sleep):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=5)
        api.list_orders.return_value = []
        api.list_positions.return_value = []
        notifier = MagicMock()
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, notifier=notifier)

        pm.check_market_hours()

        calls = [c[0][0] for c in notifier.send_message.call_args_list]
        assert "Stopping new positions (5min to close)" in calls
        assert "Closing all positions (5min to close)" in calls

    def test_stop_new_notifies_only_once(self):
        api = MagicMock()
        api.get_clock.return_value = self._make_clock(is_open=True, minutes_to_close=15)
        notifier = MagicMock()
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, notifier=notifier)

        pm.check_market_hours()
        pm.check_market_hours()

        assert notifier.send_message.call_count == 1

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_retry(self, mock_sleep):
        api = MagicMock()
        api.list_orders.return_value = []
        api.list_positions.side_effect = [
            [MagicMock()],
            [MagicMock()],
            [],
        ]
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10)

        pm.close_all_positions()

        assert api.close_all_positions.call_count == 3
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

    def test_close_all_survives_connection_error(self):
        api = MagicMock()
        api.cancel_all_orders.side_effect = RequestsConnectionError("Remote end closed connection")
        pm = PositionManager(api)

        pm.close_all_positions()

        api.close_all_positions.assert_not_called()

    def test_eod_time_overrides_close(self):
        api = MagicMock()
        clock = MagicMock()
        clock.is_open = True
        clock.timestamp = datetime(2026, 3, 12, 12, 0, 0)
        clock.next_close = datetime(2026, 3, 12, 16, 0, 0)
        api.get_clock.return_value = clock
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, eod_time=dt_time(13, 0))

        pm.check_market_hours()

        assert pm.market_close_time == datetime(2026, 3, 12, 13, 0, 0)
        assert pm.accepting_new_positions is True

    def test_eod_time_triggers_stop_new(self):
        api = MagicMock()
        clock = MagicMock()
        clock.is_open = True
        clock.timestamp = datetime(2026, 3, 12, 12, 45, 0)
        clock.next_close = datetime(2026, 3, 12, 16, 0, 0)
        api.get_clock.return_value = clock
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, eod_time=dt_time(13, 0))

        pm.check_market_hours()

        assert pm.accepting_new_positions is False
        assert pm.positions_closed_for_day is False

    def test_eod_time_after_close_ignored(self):
        api = MagicMock()
        clock = MagicMock()
        clock.is_open = True
        clock.timestamp = datetime(2026, 3, 12, 15, 0, 0)
        clock.next_close = datetime(2026, 3, 12, 16, 0, 0)
        api.get_clock.return_value = clock
        pm = PositionManager(api, stop_new_minutes=20, close_all_minutes=10, eod_time=dt_time(17, 0))

        pm.check_market_hours()

        assert pm.market_close_time == datetime(2026, 3, 12, 16, 0, 0)

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_retry_survives_connection_error(self, mock_sleep):
        api = MagicMock()
        api.list_orders.return_value = []
        api.list_positions.side_effect = [
            ProtocolError("Connection aborted"),
            [],
        ]
        pm = PositionManager(api)

        pm.close_all_positions()

        assert api.list_positions.call_count == 2

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_waits_for_orders_to_clear_before_closing(self, mock_sleep):
        """Reproduces the bug where close_all_positions was called before
        cancel_all_orders settled, causing Alpaca to reject closes for shares
        still tied up in pending-cancel OCO orders."""
        api = MagicMock()
        call_log = []
        api.cancel_all_orders.side_effect = lambda: call_log.append("cancel_all_orders")
        api.list_orders.side_effect = lambda **kw: (
            call_log.append("list_orders"),
            [MagicMock(), MagicMock()] if call_log.count("list_orders") < 3 else [],
        )[1]
        api.close_all_positions.side_effect = lambda: call_log.append("close_all_positions")
        api.list_positions.return_value = []
        pm = PositionManager(api)

        pm.close_all_positions()

        cancel_idx = call_log.index("cancel_all_orders")
        first_close_idx = call_log.index("close_all_positions")
        last_orders_before_close = max(
            i for i, name in enumerate(call_log[:first_close_idx]) if name == "list_orders"
        )
        assert cancel_idx < last_orders_before_close < first_close_idx

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_retries_when_positions_remain(self, mock_sleep):
        """When close_all_positions returns 207 with rejections (positions stay
        open), the retry loop must call close_all_positions again, not just
        poll list_positions."""
        api = MagicMock()
        api.list_orders.return_value = []
        api.list_positions.side_effect = [[MagicMock()], [MagicMock()], []]
        pm = PositionManager(api)

        pm.close_all_positions()

        assert api.close_all_positions.call_count == 3

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_survives_apierror_on_close(self, mock_sleep):
        api = MagicMock()
        api.list_orders.return_value = []
        api.close_all_positions.side_effect = APIError({"message": "boom", "code": 500})
        api.list_positions.return_value = [MagicMock()]
        notifier = MagicMock()
        pm = PositionManager(api, notifier=notifier)

        pm.close_all_positions()

        assert api.close_all_positions.call_count == 5
        notifier.send_message.assert_called_once()
        assert "EOD close failed" in notifier.send_message.call_args[0][0]

    @patch("relay_client.position_manager.time.sleep")
    def test_close_all_survives_apierror_on_cancel(self, mock_sleep):
        api = MagicMock()
        api.cancel_all_orders.side_effect = APIError({"message": "boom", "code": 500})
        pm = PositionManager(api)

        pm.close_all_positions()

        api.close_all_positions.assert_not_called()

    @patch("relay_client.position_manager.time.sleep")
    @patch("relay_client.position_manager.time.monotonic")
    def test_wait_for_orders_clear_times_out(self, mock_monotonic, mock_sleep):
        api = MagicMock()
        api.list_orders.return_value = [MagicMock()]
        api.list_positions.return_value = []
        mock_monotonic.side_effect = [0.0, 0.0, 5.0, 11.0, 12.0, 13.0]
        pm = PositionManager(api)

        pm.close_all_positions()

        api.close_all_positions.assert_called()
