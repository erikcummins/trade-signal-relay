# Trade Signal Relay — Development Plan

Reference documents:
- **Design doc**: `docs/design-plan.md`
- **Client reference**: `news_trader/src/live_real_money.py` — the relay client mirrors this script's structure for Alpaca trading, EOD close logic, Discord notifications, and main loop. The key difference: no algo/sentiment analysis — signals come from the relay server and are percentage-based TP/SL.

---

## Sprint 1: Shared Foundations

- [ ] **Step 1.1 — Message schemas (`shared/messages.py`)**
  - [ ] Define dataclasses/Pydantic models for all message types:
    - `AuthPublisher`: `{"type": "auth", "publisher_key": "..."}`
    - `AuthSubscriber`: `{"type": "auth", "subscriber_key": "..."}`
    - `AuthResult`: `{"type": "auth_result", "success": bool}`
    - `Signal`: `{"type": "signal", "signal_id": uuid, "action": "open", "ticker", "side", "tp_percent", "sl_percent", "timestamp"}`
    - `Error`: `{"type": "error", "message": "..."}`
    - `Ping`: `{"type": "ping"}`
  - [ ] `serialize()` and `deserialize()` helpers for JSON round-tripping
  - [ ] Validation: `tp_percent > 0`, `sl_percent > 0`, `side` in `["buy", "sell"]`, `action` in `["open"]`
  - [ ] Unit tests for serialization, deserialization, and validation

- [ ] **Step 1.2 — Auth helpers (`shared/auth.py`)**
  - [ ] Key format validation:
    - Publisher keys: `pub_<algo_id>_<random>` — regex validator
    - Subscriber keys: `sub_<user_id>_<random>` — regex validator
  - [ ] `extract_algo_id(publisher_key)` → returns algo_id portion
  - [ ] `extract_user_id(subscriber_key)` → returns user_id portion
  - [ ] Unit tests for valid/invalid key formats

---

## Sprint 2: Relay Publisher

- [ ] **Step 2.1 — SignalPublisher class (`relay_publisher/publisher.py`)**
  - [ ] Constructor: `SignalPublisher(server_url, publisher_key)`
  - [ ] `connect()`: opens WebSocket, sends auth message, waits for `auth_result`
  - [ ] `publish_open(ticker, side, tp_percent, sl_percent)`: builds `Signal` message with UUID + timestamp, sends over WebSocket
  - [ ] Runs WebSocket in a background thread (non-blocking to the algo)
  - [ ] Auto-reconnect on disconnect with exponential backoff
  - [ ] Thread-safe message sending
  - [ ] `disconnect()`: clean shutdown
  - [ ] Unit tests: message construction, key validation, reconnect logic (mock WebSocket)

- [ ] **Step 2.2 — Package setup (`relay_publisher/__init__.py`)**
  - [ ] Export `SignalPublisher` from package

---

## Sprint 3: Relay Server — Lambda Handlers & Auth

- [ ] **Step 3.1 — DynamoDB table schemas**
  - [ ] `relay-connections` — PK: `connection_id`, Attributes: `role`, `key`, `algo_id`, `user_id`, `connected_at`
  - [ ] `relay-access` — PK: `subscriber_key`, Attributes: `allowed_algos` (list of algo_ids)
  - [ ] `relay-signals` — PK: `algo_id`, SK: `timestamp#signal_id`, TTL: 24 hours

- [ ] **Step 3.2 — Lambda handlers (`relay_server/server.py`)**
  - [ ] `handle_connect` (route: `$connect`): stores connection_id in DynamoDB, returns 200
  - [ ] `handle_disconnect` (route: `$disconnect`): removes connection from DynamoDB
  - [ ] `handle_message` (route: `$default`): routes based on message `type`:
    - `auth` (publisher): validate key format, store connection with `role=publisher` and `algo_id`
    - `auth` (subscriber): validate key against `relay-access` table, store connection with `role=subscriber` and allowed algos
    - `signal`: verify sender is authenticated publisher → store in history → fan out to authorized subscribers via API Gateway Management API
  - [ ] `handle_ping`: keepalive response
  - [ ] Unit tests: auth validation, signal routing logic, fan-out to correct subscribers (mock DynamoDB + API Gateway)

- [ ] **Step 3.3 — Auth module (`relay_server/auth.py`)**
  - [ ] `validate_publisher_key(key, dynamo_table)` → checks key format + existence
  - [ ] `validate_subscriber_key(key, dynamo_table)` → checks key format + returns allowed algo list
  - [ ] `get_subscribers_for_algo(algo_id, connections_table)` → returns list of connection_ids
  - [ ] Unit tests with mocked DynamoDB

- [ ] **Step 3.4 — Reconnect / missed signal handler**
  - [ ] On subscriber auth, accept optional `last_signal_id` field
  - [ ] Query `relay-signals` table for signals after `last_signal_id` for subscriber's allowed algos
  - [ ] Send missed signals to subscriber in order
  - [ ] Unit tests for signal history query and replay logic

---

## Sprint 4: AWS Deployment

- [ ] **Step 4.1 — Deployment script (`infra/deploy.sh`)**
  Single script — no manual AWS console work required. Uses AWS CLI:
  - [ ] Package Lambda code (zip `relay_server/` + `shared/` + dependencies)
  - [ ] Create IAM role for Lambda with DynamoDB + API Gateway Management permissions
  - [ ] Create DynamoDB tables (`relay-connections`, `relay-access`, `relay-signals`) with TTL enabled
  - [ ] Create Lambda function (or update if exists)
  - [ ] Create API Gateway WebSocket API with routes (`$connect`, `$disconnect`, `$default`)
  - [ ] Create API Gateway integration pointing routes to Lambda
  - [ ] Create deployment + stage (e.g., `prod`)
  - [ ] Grant API Gateway permission to invoke Lambda
  - [ ] Output the WebSocket URL (`wss://xxxx.execute-api.region.amazonaws.com/prod`)

- [ ] **Step 4.2 — Management subcommands**
  - [ ] `./infra/deploy.sh` — full deploy (idempotent, safe to re-run)
  - [ ] `./infra/deploy.sh update` — update Lambda code only (fast iteration)
  - [ ] `./infra/deploy.sh add-subscriber <subscriber_key> <algo_id1,algo_id2>` — add access mapping
  - [ ] `./infra/deploy.sh remove-subscriber <subscriber_key>` — remove access
  - [ ] `./infra/deploy.sh add-publisher <algo_id>` — generate and store a publisher key
  - [ ] `./infra/deploy.sh status` — show deployed resources and connection count

- [ ] **Step 4.3 — Teardown script (`infra/teardown.sh`)**
  - [ ] Deletes API Gateway, Lambda, IAM role, DynamoDB tables
  - [ ] Confirms before deletion

---

## Sprint 5: Relay Client — Trading Core

**Reference: `news_trader/src/live_real_money.py`** — the client mirrors this script's architecture.

- [ ] **Step 5.1 — Config loader (`relay_client/config.py`)**
  - [ ] Load `config.yaml`: relay_server, access_key, alpaca credentials, trading params, eod params, discord params
  - [ ] Validate required fields, set defaults for optional ones
  - [ ] Support `--config` CLI arg
  - [ ] Unit tests for config loading and validation

- [ ] **Step 5.2 — Trade executor (`relay_client/trader.py`)**
  - [ ] `AlpacaTrader` class wrapping `alpaca_trade_api.REST`
  - [ ] `execute_signal(ticker, side, tp_percent, sl_percent, position_size)`:
    1. Check for existing position in ticker → skip if exists
    2. `get_latest_trade(ticker)` → current price
    3. Calculate TP/SL prices from percentages:
       - BUY: `tp = price * (1 + tp_percent/100)`, `sl = price * (1 - sl_percent/100)`
       - SELL: `tp = price * (1 - tp_percent/100)`, `sl = price * (1 + sl_percent/100)`
    4. Calculate shares: `int(position_size / price)`
    5. Submit bracket order (mirror lines 544-554 of reference `open_position()`)
    6. Return order details for notification
  - [ ] Unit tests: TP/SL calculation, share calculation, duplicate position check (mock Alpaca API)

- [ ] **Step 5.3 — Position manager (`relay_client/position_manager.py`)**
  Directly mirrors `check_market_hours()` (lines 146-194) and `close_all_positions()` (lines 196-250) from `live_real_money.py`:
  - [ ] `check_market_hours(trading_api)`: get clock, update close time, manage flags with configurable thresholds from config.yaml `eod` section
  - [ ] `close_all_positions(trading_api)`: cancel open orders → close positions → wait + retry loop until confirmed closed or market closes
  - [ ] Trading flags: `accepting_new_positions`, `positions_closed_for_day`, `market_close_time` — identical to reference
  - [ ] Unit tests: flag transitions at various times, close retry logic (mock Alpaca API)

---

## Sprint 6: Relay Client — Connectivity & Notifications

- [ ] **Step 6.1 — Discord notifications (`relay_client/discord_bot.py`)**
  Same `SyncDiscordBot` pattern as reference (lines 106-112 of `live_real_money.py`):
  - [ ] discord.py running in background thread with sync `send_message()` wrapper
  - [ ] No-op notifier when discord config not provided
  - [ ] Notifications: connected, signal received, position opened (with details), position closed, errors
  - [ ] Unit tests: no-op behavior when unconfigured, message formatting

- [ ] **Step 6.2 — WebSocket client (`relay_client/client.py`)**
  - [ ] `RelayClient` class — connects to relay server, handles signals
  - [ ] `connect()`: open WebSocket, send auth with `subscriber_key`, wait for `auth_result`
  - [ ] On `signal` message: pass to `AlpacaTrader.execute_signal()`
  - [ ] Auto-reconnect with exponential backoff
  - [ ] Send `last_signal_id` on reconnect for missed signal recovery
  - [ ] Track `last_signal_id` locally
  - [ ] Unit tests: auth flow, signal handling dispatch, reconnect behavior (mock WebSocket)

- [ ] **Step 6.3 — Main entry point (`relay_client/__init__.py` + CLI)**
  Main loop mirrors `live_real_money.py` `run()` method (lines 577-632 of reference):
  - [ ] WebSocket client runs in background thread receiving signals
  - [ ] Main thread runs EOD check loop: `check_market_hours()` → flag management → `sleep(5)`
  - [ ] Market closed: reset flags, sleep 60, continue
  - [ ] CLI: `trade-relay-client --config config.yaml`
  - [ ] Graceful shutdown on Ctrl+C (identical to reference lines 620-624)
  - [ ] Unit tests: main loop lifecycle (market open/close transitions)

---

## Sprint 7: Packaging & Integration

- [ ] **Step 7.1 — setup.py / pyproject.toml**
  - [ ] Package name: `trade-signal-relay`
  - [ ] Installable extras: `[publisher]` (just publisher lib), `[client]` (client with all deps)
  - [ ] Console script entry point: `trade-relay-client` → `relay_client.__main__:main`

- [ ] **Step 7.2 — Example configs**
  - [ ] `config.example.yaml` with all fields documented
  - [ ] `.env.example` for publisher keys

- [ ] **Step 7.3 — requirements.txt**
  - [ ] Server: `boto3`
  - [ ] Publisher: `websockets`
  - [ ] Client: `websockets`, `alpaca-trade-api`, `discord.py`, `pyyaml`, `python-dotenv`
  - [ ] Dev: `pytest`, `pytest-asyncio`, `moto` (for DynamoDB mocks)

- [ ] **Step 7.4 — End-to-end test script**
  - [ ] Local WebSocket echo server for testing without AWS
  - [ ] Publisher sends signal → server routes → client receives and validates
  - [ ] Full message flow verification

- [ ] **Step 7.5 — AWS integration smoke test**
  - [ ] Connect publisher after deploy, send test signal
  - [ ] Connect subscriber, verify receipt
  - [ ] Verify signal history in DynamoDB
  - [ ] Runnable via `./infra/deploy.sh test`
