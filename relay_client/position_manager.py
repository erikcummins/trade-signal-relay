import logging
import time

from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import ProtocolError

log = logging.getLogger("relay_client")


class PositionManager:
    def __init__(self, api, stop_new_minutes: int = 20, close_all_minutes: int = 10, notifier=None):
        self.api = api
        self.stop_new_minutes = stop_new_minutes
        self.close_all_minutes = close_all_minutes
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None
        self.notifier = notifier

    def check_market_hours(self) -> bool:
        clock = self.api.get_clock()
        if not clock.is_open:
            return False

        self.market_close_time = clock.next_close
        minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60

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
            self.api.close_all_positions()
        except (ConnectionError, ProtocolError, RequestException) as e:
            log.error("Failed to close positions: %s", e)
            return

        for _ in range(5):
            time.sleep(1)
            try:
                positions = self.api.list_positions()
            except (ConnectionError, ProtocolError, RequestException):
                continue
            if not positions:
                return

    def reset(self):
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None
