from datetime import datetime, time as dt_time
from unittest.mock import MagicMock

import pytest

from relay_client import per_position_eod
from relay_client.per_position_eod import PerPositionEodCloser, parse_eod_time


def _clock(time_str):
    c = MagicMock()
    c.timestamp = datetime.strptime(f"2026-04-28 {time_str}", "%Y-%m-%d %H:%M")
    return c


def _position(qty=100, qty_available=None):
    p = MagicMock()
    p.qty = str(qty)
    p.qty_available = str(qty if qty_available is None else qty_available)
    return p


class TestPerPositionEodCloser:
    def test_no_op_when_no_entries(self):
        api = MagicMock()
        closer = PerPositionEodCloser(api)
        closer.check_and_close()
        api.get_clock.assert_not_called()

    def test_does_not_close_before_eod(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("13:00")
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_not_called()
        api.get_position.assert_not_called()

    def test_closes_position_at_or_after_eod(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = [MagicMock(id="ord-1"), MagicMock(id="ord-2")]
        api.get_position.return_value = _position()
        notifier = MagicMock()
        closer = PerPositionEodCloser(api, notifier=notifier, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.list_orders.assert_called_once_with(status="open", symbols=["AAPL"], nested=True)
        assert api.cancel_order.call_count == 2
        api.close_position.assert_called_once_with("AAPL")
        notifier.send_message.assert_called_once_with("Closed AAPL (per-position EOD)")

    def test_drops_entry_after_close(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        api.get_position.return_value = _position()
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()
        api.close_position.reset_mock()
        closer.check_and_close()

        api.close_position.assert_not_called()

    def test_skips_when_position_already_gone(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.return_value = _clock("16:00")
        api.get_position.side_effect = APIError({"message": "position not found"})
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_not_called()
        closer.check_and_close()
        api.get_position.assert_called_once()

    def test_get_position_transient_failure_keeps_entry(self):
        from requests.exceptions import ConnectionError as RequestsConnectionError
        api = MagicMock()
        api.get_clock.return_value = _clock("16:00")
        api.get_position.side_effect = RequestsConnectionError("net down")
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_not_called()
        assert closer._entries == {"AAPL": dt_time(15, 55)}

    def test_only_closes_targeted_ticker(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        api.get_position.return_value = _position()
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))
        closer.register("TSLA", dt_time(16, 30))

        closer.check_and_close()

        api.close_position.assert_called_once_with("AAPL")
        assert closer._entries == {"TSLA": dt_time(16, 30)}

    def test_close_failure_keeps_entry_for_retry(self):
        from requests.exceptions import ConnectionError as RequestsConnectionError
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        api.get_position.return_value = _position()
        api.close_position.side_effect = RequestsConnectionError("net down")
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert closer._entries == {"AAPL": dt_time(15, 55)}

    def test_api_error_on_close_keeps_entry_for_retry(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        api.get_position.return_value = _position()
        api.close_position.side_effect = APIError({"message": "insufficient qty available for order (requested: 365, available: 0)"})
        closer = PerPositionEodCloser(api, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert closer._entries == {"AAPL": dt_time(15, 55)}

    def test_waits_until_qty_fully_available_before_close(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = [MagicMock(id="ord-1")]
        api.get_position.side_effect = [
            _position(qty=100, qty_available=100),
            _position(qty=100, qty_available=0),
            _position(qty=100, qty_available=0),
            _position(qty=100, qty_available=100),
        ]
        sleeps = []
        closer = PerPositionEodCloser(api, sleep=lambda s: sleeps.append(s))
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert api.get_position.call_count == 4
        api.close_position.assert_called_once_with("AAPL")
        assert len(sleeps) == 2

    def test_close_proceeds_if_qty_never_available(self, monkeypatch):
        monkeypatch.setattr(per_position_eod, "CANCEL_SETTLE_TIMEOUT", 0.01)
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = [MagicMock(id="ord-1")]
        api.get_position.return_value = _position(qty=100, qty_available=0)
        closer = PerPositionEodCloser(api, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_called_once_with("AAPL")

    def test_cancel_failure_does_not_abort_close(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = [MagicMock(id="ord-1"), MagicMock(id="ord-2")]
        api.get_position.return_value = _position()
        api.cancel_order.side_effect = [APIError({"message": "already filled"}), None]
        closer = PerPositionEodCloser(api, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert api.cancel_order.call_count == 2
        api.close_position.assert_called_once_with("AAPL")

    def test_list_orders_failure_skips_settle_and_attempts_close(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.side_effect = APIError({"message": "boom"})
        api.get_position.return_value = _position()
        closer = PerPositionEodCloser(api, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.cancel_order.assert_not_called()
        api.close_position.assert_called_once_with("AAPL")

    def test_get_position_failure_during_settle_proceeds_to_close(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        api.get_position.side_effect = [_position(), APIError({"message": "boom"})]
        closer = PerPositionEodCloser(api, sleep=lambda _: None)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_called_once_with("AAPL")

    def test_clock_api_error_does_not_clear_entries(self):
        from alpaca_trade_api.rest import APIError
        api = MagicMock()
        api.get_clock.side_effect = APIError({"message": "boom"})
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert closer._entries == {"AAPL": dt_time(15, 55)}

    def test_clock_failure_does_not_clear_entries(self):
        from requests.exceptions import ConnectionError as RequestsConnectionError
        api = MagicMock()
        api.get_clock.side_effect = RequestsConnectionError("net down")
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        assert closer._entries == {"AAPL": dt_time(15, 55)}

    def test_parse_eod_time(self):
        assert parse_eod_time("15:55") == dt_time(15, 55)
        assert parse_eod_time("09:00") == dt_time(9, 0)

    def test_parse_eod_time_invalid(self):
        with pytest.raises(ValueError):
            parse_eod_time("nope")
