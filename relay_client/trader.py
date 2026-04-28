import time

import alpaca_trade_api as tradeapi

from shared.messages import Signal


FILL_TIMEOUT_SECONDS = 10
FILL_POLL_INTERVAL = 0.5
MIN_SHORT_TP_PRICE = 0.01


class FillError(RuntimeError):
    pass


class ExitOrderError(RuntimeError):
    pass


class AlpacaTrader:
    def __init__(self, api_key: str, secret_key: str, paper: bool, position_size: int):
        self.position_size = position_size
        self._api_key = api_key
        self._secret_key = secret_key
        self._base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.api = tradeapi.REST(api_key, secret_key, self._base_url)

    def reset_connection(self):
        self.api = tradeapi.REST(self._api_key, self._secret_key, self._base_url)

    def has_position(self, ticker: str) -> bool:
        try:
            self.api.get_position(ticker)
            return True
        except Exception:
            return False

    def execute_signal(self, signal: Signal, position_size: int | None = None) -> dict | None:
        if self.has_position(signal.ticker):
            return None

        size = position_size if position_size is not None else self.position_size
        price = float(self.api.get_latest_trade(signal.ticker).price)
        shares = int(size / price)
        if shares <= 0:
            return None

        order = self.api.submit_order(
            symbol=signal.ticker,
            qty=shares,
            side=signal.side,
            type="market",
            time_in_force="day",
        )

        fill_price, filled_qty = self._wait_for_fill(order.id)

        tp_price, sl_price = self._calculate_tp_sl(
            fill_price, signal.side, signal.tp_percent, signal.sl_percent
        )

        exit_side = "sell" if signal.side == "buy" else "buy"
        try:
            if tp_price is None:
                self.api.submit_order(
                    symbol=signal.ticker,
                    qty=filled_qty,
                    side=exit_side,
                    type="stop",
                    time_in_force="day",
                    stop_price=sl_price,
                )
            else:
                self.api.submit_order(
                    symbol=signal.ticker,
                    qty=filled_qty,
                    side=exit_side,
                    type="limit",
                    time_in_force="day",
                    order_class="oco",
                    take_profit={"limit_price": tp_price},
                    stop_loss={"stop_price": sl_price},
                )
        except Exception as e:
            self._kill_position(signal.ticker)
            raise ExitOrderError(f"Failed to set TP/SL for {signal.ticker}: {e}") from e

        return {
            "ticker": signal.ticker,
            "side": signal.side,
            "shares": filled_qty,
            "entry_price": fill_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
        }

    def _wait_for_fill(self, order_id: str) -> tuple[float, int]:
        deadline = time.monotonic() + FILL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(FILL_POLL_INTERVAL)
            o = self.api.get_order(order_id)
            status = str(o.status)
            if status == "filled":
                return float(o.filled_avg_price), int(float(o.filled_qty))
            if status in ("canceled", "expired", "rejected"):
                raise FillError(f"Market order {order_id} ended with status {status}")
        try:
            self.api.cancel_order(order_id)
        except Exception:
            pass
        raise FillError(f"Market order {order_id} did not fill within {FILL_TIMEOUT_SECONDS}s")

    @staticmethod
    def _calculate_tp_sl(fill_price: float, side: str, tp_pct: float | None, sl_pct: float) -> tuple[float | None, float]:
        if side == "buy":
            sl = fill_price * (1 - sl_pct / 100)
            tp = fill_price * (1 + tp_pct / 100) if tp_pct is not None else None
        else:
            sl = fill_price * (1 + sl_pct / 100)
            tp = fill_price * (1 - tp_pct / 100) if tp_pct is not None else None
        sl = round(sl, 2)
        if tp is not None:
            tp = round(tp, 2)
            if side == "sell" and tp < MIN_SHORT_TP_PRICE:
                tp = MIN_SHORT_TP_PRICE
        return tp, sl

    def _kill_position(self, ticker: str):
        try:
            self.api.close_position(ticker)
        except Exception:
            pass
