# Architecture

## Package Layout

```
shared/                 # stdlib only — no external deps
  messages.py           # Dataclasses: AuthPublisher, AuthSubscriber, AuthResult, Signal, Error, Ping
  auth.py               # Key validation (regex) + extract_algo_id / extract_user_id

relay_publisher/        # depends on: shared, websockets
  publisher.py          # SignalPublisher class — algo repos pip-install this

relay_server/           # depends on: shared, boto3 — deployed as Lambda
  server.py             # handler(event, context) — single Lambda entry point
  auth.py               # DynamoDB auth: validate_publisher, validate_subscriber, get_subscribers_for_algo

relay_client/           # depends on: shared, websockets, alpaca-trade-api, discord.py, pyyaml
  config.py             # load_config(path) → Config dataclass
  trader.py             # AlpacaTrader — execute_signal() submits bracket orders
  position_manager.py   # EOD close logic — check_market_hours(), close_all_positions()
  discord_bot.py        # SyncDiscordBot / NoOpNotifier
  client.py             # RelayClient — WebSocket subscriber
  __main__.py           # CLI entry point: python -m relay_client --config config.yaml

infra/
  deploy.sh             # AWS deploy (Lambda + API Gateway + DynamoDB)
  teardown.sh           # Deletes all AWS resources

tests/
  test_shared.py        # Message serialization, validation, key format
  test_publisher.py     # SignalPublisher: message construction, auth, reconnect
  test_server.py        # Lambda handlers with moto (DynamoDB mock)
  test_client_trading.py    # Config, AlpacaTrader, PositionManager
  test_client_connectivity.py  # RelayClient, NoOpNotifier, main loop
  test_e2e.py           # Local WebSocket relay: publisher → server → subscriber
  test_smoke_aws.py     # Skipped unless RELAY_WS_URL is set
```

## Signal Flow

```
Algo → SignalPublisher.publish_open()
     → WebSocket → API Gateway
     → Lambda handler (relay_server.server.handler)
     → DynamoDB (store signal history)
     → Fan out via post_to_connection to subscribers
     → RelayClient._receive_loop → on_signal callback
     → AlpacaTrader.execute_signal() → bracket order on Alpaca
```

## Key Pattern: Background Thread + Asyncio

Both `SignalPublisher` and `RelayClient` use the same pattern:
- `connect()` starts a daemon thread running `asyncio.new_event_loop()`
- The thread runs a `_connection_loop` with auto-reconnect (exponential backoff, 1s→30s cap)
- `threading.Event` signals when connected
- Thread-safe communication via `queue.Queue` (publisher) or callback (client)
- `disconnect()` sets a stop flag and joins the thread

## Message Protocol

All WebSocket messages are JSON with a `type` field. Use `shared.messages.serialize()` and `deserialize()` for all message handling. The `deserialize()` function dispatches on `type` and validates signal fields.

## Auth Key Format

- Publisher: `pub_<algo_id>_<random>` (e.g., `pub_algo1_abc123`)
- Subscriber: `sub_<user_id>_<random>` (e.g., `sub_alice_x8k2`)
- Regex: alphanumeric only for each segment, validated by `shared.auth`
