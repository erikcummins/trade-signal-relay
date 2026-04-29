from datetime import datetime, time as dt_time
from unittest.mock import MagicMock

import pytest

from relay_client.per_position_eod import PerPositionEodCloser, parse_eod_time


def _clock(time_str):
    c = MagicMock()
    c.timestamp = datetime.strptime(f"2026-04-28 {time_str}", "%Y-%m-%d %H:%M")
    return c


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
        notifier = MagicMock()
        closer = PerPositionEodCloser(api, notifier=notifier)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.list_orders.assert_called_once_with(status="open", symbols="AAPL")
        assert api.cancel_order.call_count == 2
        api.close_position.assert_called_once_with("AAPL")
        notifier.send_message.assert_called_once_with("Closed AAPL (per-position EOD)")

    def test_drops_entry_after_close(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()
        api.close_position.reset_mock()
        closer.check_and_close()

        api.close_position.assert_not_called()

    def test_skips_when_position_already_gone(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("16:00")
        api.get_position.side_effect = Exception("position not found")
        closer = PerPositionEodCloser(api)
        closer.register("AAPL", dt_time(15, 55))

        closer.check_and_close()

        api.close_position.assert_not_called()
        closer.check_and_close()
        api.get_position.assert_called_once()

    def test_only_closes_targeted_ticker(self):
        api = MagicMock()
        api.get_clock.return_value = _clock("15:55")
        api.list_orders.return_value = []
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
        api.close_position.side_effect = RequestsConnectionError("net down")
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
