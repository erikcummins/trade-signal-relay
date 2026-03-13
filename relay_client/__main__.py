import argparse
import logging
import time

from relay_client.config import load_config
from relay_client.trader import AlpacaTrader
from relay_client.position_manager import PositionManager
from relay_client.discord_bot import create_notifier
from relay_client.client import RelayClient

import alpaca_trade_api as tradeapi

log = logging.getLogger("relay_client")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)

    trader = AlpacaTrader(
        api_key=config.alpaca.api_key,
        secret_key=config.alpaca.secret_key,
        paper=config.alpaca.paper,
        position_size=config.trading.position_size,
    )

    notifier = create_notifier(config.discord)

    base_url = "https://paper-api.alpaca.markets" if config.alpaca.paper else "https://api.alpaca.markets"
    api = tradeapi.REST(config.alpaca.api_key, config.alpaca.secret_key, base_url)
    position_manager = PositionManager(
        api,
        stop_new_minutes=config.eod.stop_new_positions_minutes,
        close_all_minutes=config.eod.close_all_minutes,
        notifier=notifier,
    )

    def on_signal(signal):
        log.info("Signal received: %s %s %s", signal.action, signal.side, signal.ticker)
        if position_manager.accepting_new_positions:
            size = config.trading.get_position_size(signal.algo_id)
            result = trader.execute_signal(signal, position_size=size)
            if result:
                msg = (
                    f"Order: {result['side']} {result['shares']} {result['ticker']} "
                    f"@ {result['entry_price']:.2f} "
                    f"TP={result['tp_price']:.2f} SL={result['sl_price']:.2f}"
                )
                log.info(msg)
                notifier.send_message(msg)
            else:
                log.info("Signal skipped: %s (existing position)", signal.ticker)
                notifier.send_message(f"Signal skipped: {signal.ticker} (existing position)")
        else:
            log.info("Signal ignored (market closing): %s", signal.ticker)
            notifier.send_message(f"Signal ignored (market closing): {signal.ticker}")

    client = RelayClient(config.relay_server, config.access_key, on_signal)
    log.info("Connecting to %s", config.relay_server)
    client.connect()
    log.info("Connected to relay")
    notifier.send_message("Connected to relay")

    market_was_open = False
    try:
        while True:
            market_open = position_manager.check_market_hours()
            if not market_open:
                if market_was_open:
                    position_manager.reset()
                    log.info("Market closed")
                    notifier.send_message("Market closed")
                market_was_open = False
                time.sleep(60)
            else:
                if not market_was_open:
                    close_time = position_manager.market_close_time
                    close_str = close_time.strftime("%I:%M %p") if close_time else ""
                    msg = f"Market open (closes {close_str})"
                    log.info(msg)
                    notifier.send_message(msg)
                market_was_open = True
                time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down")
        client.disconnect()
        notifier.shutdown()


if __name__ == "__main__":
    main()
