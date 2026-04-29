import logging
import time
from datetime import datetime, time as dt_time

from alpaca_trade_api.rest import APIError
from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import ProtocolError

log = logging.getLogger("relay_client")

CANCEL_SETTLE_TIMEOUT = 10.0
CANCEL_SETTLE_INTERVAL = 0.5


def parse_eod_time(value: str) -> dt_time:
    return datetime.strptime(value, "%H:%M").time()


class PerPositionEodCloser:
    def __init__(self, api, notifier=None, sleep=time.sleep):
        self.api = api
        self.notifier = notifier
        self._sleep = sleep
        self._entries: dict[str, dt_time] = {}

    def register(self, ticker: str, eod_time: dt_time):
        self._entries[ticker] = eod_time

    def clear(self):
        self._entries.clear()

    def check_and_close(self):
        if not self._entries:
            return
        try:
            clock = self.api.get_clock()
        except (ConnectionError, ProtocolError, RequestException, APIError) as e:
            log.warning("Per-position EOD: clock fetch failed: %s", e)
            return
        now = clock.timestamp.time()
        for ticker in list(self._entries.keys()):
            if now < self._entries[ticker]:
                continue
            has_pos = self._has_position(ticker)
            if has_pos is None:
                continue
            if not has_pos:
                self._entries.pop(ticker, None)
                continue
            if self._close(ticker):
                self._entries.pop(ticker, None)
                msg = f"Closed {ticker} (per-position EOD)"
                log.info(msg)
                if self.notifier:
                    self.notifier.send_message(msg)

    def _has_position(self, ticker):
        try:
            self.api.get_position(ticker)
            return True
        except APIError as e:
            if _is_not_found(e):
                return False
            log.warning("Per-position EOD: get_position(%s) failed: %s", ticker, e)
            return None
        except (ConnectionError, ProtocolError, RequestException) as e:
            log.warning("Per-position EOD: get_position(%s) failed: %s", ticker, e)
            return None
        except Exception as e:
            log.warning("Per-position EOD: get_position(%s) unexpected error: %s", ticker, e)
            return False

    def _close(self, ticker):
        try:
            self._cancel_open_orders(ticker)
            self._wait_for_orders_clear(ticker)
            self.api.close_position(ticker)
            return True
        except (ConnectionError, ProtocolError, RequestException, APIError) as e:
            log.warning("Per-position EOD close failed for %s: %s", ticker, e)
            return False
        except Exception as e:
            log.warning("Per-position EOD close unexpected error for %s: %s", ticker, e)
            return False

    def _cancel_open_orders(self, ticker):
        try:
            orders = self.api.list_orders(status="open", symbols=ticker)
        except (ConnectionError, ProtocolError, RequestException, APIError) as e:
            log.warning("Per-position EOD: list_orders(%s) failed: %s", ticker, e)
            return
        for o in orders or []:
            order_id = getattr(o, "id", None)
            if not order_id:
                continue
            try:
                self.api.cancel_order(order_id)
            except (ConnectionError, ProtocolError, RequestException, APIError) as e:
                log.warning("Per-position EOD: cancel_order(%s) failed: %s", order_id, e)
            except Exception as e:
                log.warning("Per-position EOD: cancel_order(%s) unexpected: %s", order_id, e)

    def _wait_for_orders_clear(self, ticker):
        deadline = time.monotonic() + CANCEL_SETTLE_TIMEOUT
        while True:
            try:
                remaining = self.api.list_orders(status="open", symbols=ticker)
            except (ConnectionError, ProtocolError, RequestException, APIError) as e:
                log.warning("Per-position EOD: list_orders(%s) during settle failed: %s", ticker, e)
                return
            if not remaining:
                return
            if time.monotonic() >= deadline:
                log.warning("Per-position EOD: %d orders still open for %s after %.1fs", len(remaining), ticker, CANCEL_SETTLE_TIMEOUT)
                return
            self._sleep(CANCEL_SETTLE_INTERVAL)


def _is_not_found(err: APIError) -> bool:
    status = getattr(err, "status_code", None)
    if status == 404:
        return True
    msg = str(err).lower()
    return "not found" in msg or "position does not exist" in msg
