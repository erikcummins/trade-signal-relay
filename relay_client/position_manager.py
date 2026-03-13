import time


class PositionManager:
    def __init__(self, api, stop_new_minutes: int = 20, close_all_minutes: int = 10):
        self.api = api
        self.stop_new_minutes = stop_new_minutes
        self.close_all_minutes = close_all_minutes
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None

    def check_market_hours(self) -> bool:
        clock = self.api.get_clock()
        if not clock.is_open:
            return False

        self.market_close_time = clock.next_close
        minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60

        if minutes_to_close <= self.stop_new_minutes:
            self.accepting_new_positions = False

        if minutes_to_close <= self.close_all_minutes and not self.positions_closed_for_day:
            self.close_all_positions()
            self.positions_closed_for_day = True

        return True

    def close_all_positions(self):
        self.api.cancel_all_orders()
        self.api.close_all_positions()

        for _ in range(5):
            time.sleep(1)
            positions = self.api.list_positions()
            if not positions:
                return

    def reset(self):
        self.accepting_new_positions = True
        self.positions_closed_for_day = False
        self.market_close_time = None
