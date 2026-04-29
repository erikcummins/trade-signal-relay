import logging
from datetime import datetime, time as dt_time

from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import ProtocolError

log = logging.getLogger("relay_client")


def parse_eod_time(value: str) -> dt_time:
    return datetime.strptime(value, "%H:%M").time()


class PerPositionEodCloser:
    def __init__(self, api, notifier=None):
        self.api = api
        self.notifier = notifier
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
        except (ConnectionError, ProtocolError, RequestException) as e:
            log.warning("Per-position EOD: clock fetch failed: %s", e)
            return
        now = clock.timestamp.time()
        for ticker in list(self._entries.keys()):
            if now < self._entries[ticker]:
                continue
            if not self._has_position(ticker):
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
        except Exception:
            return False

    def _close(self, ticker):
        try:
            orders = self.api.list_orders(status="open", symbols=ticker)
            for o in orders:
                try:
                    self.api.cancel_order(o.id)
                except Exception:
                    pass
            self.api.close_position(ticker)
            return True
        except (ConnectionError, ProtocolError, RequestException) as e:
            log.warning("Per-position EOD close failed for %s: %s", ticker, e)
            return False
