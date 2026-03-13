import alpaca_trade_api as tradeapi

from shared.messages import Signal


class AlpacaTrader:
    def __init__(self, api_key: str, secret_key: str, paper: bool, position_size: int):
        self.position_size = position_size
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.api = tradeapi.REST(api_key, secret_key, base_url)

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

        if signal.side == "buy":
            tp_price = price * (1 + signal.tp_percent / 100)
            sl_price = price * (1 - signal.sl_percent / 100)
        else:
            tp_price = price * (1 - signal.tp_percent / 100)
            sl_price = price * (1 + signal.sl_percent / 100)

        self.api.submit_order(
            symbol=signal.ticker,
            qty=shares,
            side=signal.side,
            type="market",
            time_in_force="day",
            order_class="bracket",
            stop_loss={"stop_price": round(sl_price, 2)},
            take_profit={"limit_price": round(tp_price, 2)},
        )

        return {
            "ticker": signal.ticker,
            "side": signal.side,
            "shares": shares,
            "entry_price": price,
            "tp_price": round(tp_price, 2),
            "sl_price": round(sl_price, 2),
        }
