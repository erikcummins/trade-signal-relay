import logging
import time

from alpaca_trade_api.rest import APIError
from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import ProtocolError

log = logging.getLogger("relay_client")

CANCEL_SETTLE_TIMEOUT = 10.0
CANCEL_SETTLE_INTERVAL = 0.5
CLOSE_RETRY_ATTEMPTS = 5
CLOSE_RETRY_INTERVAL = 1.0


class PositionManager:
    def __init__(self, api, stop_new_minutes: int = 20, close_all_minutes: int = 10, notifier=None, eod_time=None):
        self.api = api
        self.stop_new_minutes = stop_new_minutes
        self.close_all_minutes = close_all_minutes
        self.eod_time = eod_time
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None
        self.notifier = notifier

    def check_market_hours(self) -> bool:
        clock = self.api.get_clock()
        if not clock.is_open:
            return False

        effective_close = clock.next_close
        if self.eod_time is not None:
            custom_close = clock.next_close.replace(
                hour=self.eod_time.hour,
                minute=self.eod_time.minute,
                second=0,
                microsecond=0,
            )
            if custom_close < clock.next_close:
                effective_close = custom_close

        self.market_close_time = effective_close
        minutes_to_close = (effective_close - clock.timestamp).total_seconds() / 60

        if minutes_to_close <= self.stop_new_minutes and self.accepting_new_positions:
            self.accepting_new_positions = False
            msg = f"Stopping new positions ({int(minutes_to_close)}min to close)"
            log.info(msg)
            if self.notifier:
                self.notifier.send_message(msg)

        if minutes_to_close <= self.close_all_minutes and not self.positions_closed_for_day:
            self.close_all_positions()
            self.positions_closed_for_day = True
            msg = f"Closing all positions ({int(minutes_to_close)}min to close)"
            log.info(msg)
            if self.notifier:
                self.notifier.send_message(msg)

        return True

    def close_all_positions(self):
        try:
            self.api.cancel_all_orders()
        except (ConnectionError, ProtocolError, RequestException, APIError) as e:
            log.error("Failed to cancel orders: %s", e)
            return

        self._wait_for_orders_clear()

        for attempt in range(CLOSE_RETRY_ATTEMPTS):
            try:
                self.api.close_all_positions()
            except (ConnectionError, ProtocolError, RequestException, APIError) as e:
                log.warning("close_all_positions attempt %d failed: %s", attempt + 1, e)

            time.sleep(CLOSE_RETRY_INTERVAL)
            try:
                positions = self.api.list_positions()
            except (ConnectionError, ProtocolError, RequestException, APIError) as e:
                log.warning("list_positions failed: %s", e)
                continue
            if not positions:
                return

        log.error("Positions still open after %d close attempts", CLOSE_RETRY_ATTEMPTS)
        if self.notifier:
            self.notifier.send_message(f"EOD close failed: positions still open after {CLOSE_RETRY_ATTEMPTS} attempts")

    def _wait_for_orders_clear(self):
        deadline = time.monotonic() + CANCEL_SETTLE_TIMEOUT
        while True:
            try:
                remaining = self.api.list_orders(status="open")
            except (ConnectionError, ProtocolError, RequestException, APIError) as e:
                log.warning("list_orders during settle failed: %s", e)
                return
            if not remaining:
                return
            if time.monotonic() >= deadline:
                log.warning("%d orders still open after %.1fs", len(remaining), CANCEL_SETTLE_TIMEOUT)
                return
            time.sleep(CANCEL_SETTLE_INTERVAL)

    def reset(self):
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None
