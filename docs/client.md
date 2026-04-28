# Relay Client

## Running

```bash
python -m relay_client --config config.yaml
# or after pip install:
trade-relay-client --config config.yaml
```

## Config (`relay_client/config.py`)

`load_config(path)` returns a `Config` dataclass. Required fields: `relay_server`, `access_key`, `alpaca.api_key`, `alpaca.secret_key`. See `config.example.yaml` for full schema.

Dataclasses: `Config`, `AlpacaConfig`, `TradingConfig`, `EodConfig`, `DiscordConfig`. Raises `ConfigError` on validation failure.

## Trade Executor (`relay_client/trader.py`)

`AlpacaTrader(api_key, secret_key, paper, position_size)`:
- `execute_signal(signal)` → dict with order details or None (if position already exists)
- Checks existing positions via `api.get_position()` (exception = no position)
- Sizes shares from latest trade price: `int(position_size / price)`
- Submits a plain market order, then polls `get_order` every 0.5s up to 10s for `filled` status. Cancels on timeout (`FillError`); raises on `canceled`/`expired`/`rejected`.
- TP/SL computed from `filled_avg_price` (not pre-fill quote) to avoid slippage drift:
  - BUY: `tp = fill * (1 + tp_percent/100)`, `sl = fill * (1 - sl_percent/100)`
  - SELL: `tp = fill * (1 - tp_percent/100)`, `sl = fill * (1 + sl_percent/100)`
  - Short TP clamped to `MIN_SHORT_TP_PRICE` ($0.01) when math drives it to ≤ 0
- Exit order:
  - With TP (`tp_percent` set): submits OCO (limit TP + stop SL) on the opposite side.
  - Without TP (`tp_percent=None`, e.g. EOD-close strategies): submits a stop-only order; PositionManager closes the position at EOD.
- If the exit submission fails, calls `close_position(ticker)` and raises `ExitOrderError`.

## Position Manager (`relay_client/position_manager.py`)

`PositionManager(api, stop_new_minutes=20, close_all_minutes=10)`:
- `check_market_hours()` → bool (market open). Updates flags based on time to close.
- `accepting_new_positions` — False when within `stop_new_minutes` of close
- `positions_closed_for_day` — True after `close_all_positions()` runs
- `close_all_positions()` — cancels orders, closes positions, retries 5x with 1s sleep
- `reset()` — resets all flags for new trading day

## Discord (`relay_client/discord_bot.py`)

- `SyncDiscordBot(bot_token, channel_id)` — discord.py in background thread, `send_message()` is thread-safe via `run_coroutine_threadsafe`
- `NoOpNotifier` — no-op when Discord not configured
- `create_notifier(discord_config)` — factory function

## WebSocket Client (`relay_client/client.py`)

`RelayClient(server_url, subscriber_key, on_signal_callback)`:
- Same background thread + asyncio pattern as SignalPublisher
- Sends `AuthSubscriber` with `last_signal_id` on connect/reconnect
- Calls `on_signal_callback(signal)` for each received Signal
- Auto-reconnect with exponential backoff (1s → 30s cap)

## Main Loop (`relay_client/__main__.py`)

1. Load config, create trader/position manager/notifier/relay client
2. Signal callback: execute trade if accepting, send Discord notification
3. Main loop: `check_market_hours()` every 5s when open, sleep 60s when closed
4. Reset flags on market close transition
5. Graceful shutdown on Ctrl+C
