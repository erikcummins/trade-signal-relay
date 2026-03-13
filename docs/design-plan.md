# Trade Signal Relay — Design Document

## Overview

A system that allows trading algorithms to publish trade signals to subscribers in real-time. Subscribers run a client that automatically executes trades on their Alpaca accounts mirroring the publisher's trades.

The publisher's algorithms remain private. Only minimal signal data is transmitted: action, ticker, side, and percentage-based TP/SL levels.

## Architecture

```
┌──────────────────┐     ┌──────────────────────┐     ┌──────────────────┐
│   Algo Repo A    │     │    Relay Server       │     │   Client App     │
│                  │     │    (AWS)               │     │   (Subscriber)   │
│  ┌────────────┐  │     │                       │     │                  │
│  │ Your Algo  │──┼─wss─┤  Authenticates pubs   │─wss─┤  Receives signal │
│  │            │  │     │  Authenticates subs   │     │  Gets price      │
│  │ imports    │  │     │  Routes signals       │     │  Calculates TP/SL│
│  │ publisher  │  │     │  Enforces access keys │     │  Submits bracket │
│  └────────────┘  │     │                       │     │  order on Alpaca │
└──────────────────┘     └──────────────────────┘     │                  │
                                                      │  EOD close logic │
┌──────────────────┐                                  │  Discord notifs  │
│   Algo Repo B    │──wss─────────┘                   └──────────────────┘
└──────────────────┘
```

## Repo Structure

```
trade-signal-relay/
├── relay_server/          # AWS-deployed WebSocket relay
│   ├── server.py          # WebSocket server with auth
│   ├── auth.py            # Key validation, algo-to-subscriber mapping
│   └── models.py          # Shared message schemas
├── relay_publisher/       # Pip-installable library for algos
│   ├── __init__.py
│   └── publisher.py       # SignalPublisher class
├── relay_client/          # Standalone app for subscribers
│   ├── __init__.py
│   ├── client.py          # WebSocket client, signal handler
│   ├── trader.py          # Alpaca trade execution
│   ├── position_manager.py # EOD close logic
│   └── discord_bot.py     # Discord notifications
├── shared/                # Shared between all components
│   ├── messages.py        # Message types and schemas
│   └── auth.py            # Key format, validation helpers
├── infra/                 # AWS deployment
│   └── ...
├── config.example.yaml    # Example client config
├── requirements.txt
└── setup.py               # Allows `pip install` of publisher/client
```

## Components

### 1. Signal Publisher (library)

A small library that algo repos install via pip. Provides a single class.

```python
from relay_publisher import SignalPublisher

publisher = SignalPublisher(
    server_url="wss://your-relay.example.com",
    publisher_key="pub_key_algo1"
)
publisher.connect()

# After your algo opens a position:
publisher.publish_open(
    ticker="AAPL",
    side="buy",           # "buy" or "sell"
    tp_percent=5.0,       # take profit as % from entry
    sl_percent=1.5        # stop loss as % from entry
)

# Publisher does NOT send close signals.
# Clients handle EOD closing independently.
```

**What gets published (and nothing more):**
- `action`: "open"
- `ticker`: symbol
- `side`: "buy" or "sell"
- `tp_percent`: take profit percentage
- `sl_percent`: stop loss percentage
- `timestamp`: UTC ISO timestamp
- `signal_id`: UUID for deduplication

**What is NOT published:**
- Entry price (client determines its own)
- Position size (client uses its own config)
- Strategy name, parameters, or rationale
- Close/EOD signals

**Behavior:**
- Maintains persistent WebSocket connection
- Auto-reconnects on disconnect
- Fire-and-forget — publishing does not block the algo
- Runs in a background thread so it doesn't interfere with algo's main loop

### 2. Relay Server (AWS)

A WebSocket server that authenticates publishers and subscribers, then routes signals.

**Responsibilities:**
- Authenticate publisher connections via publisher keys
- Authenticate subscriber connections via subscriber keys
- Route signals from a publisher's algo to authorized subscribers only
- Store recent signal history (last 24h) for client reconciliation on reconnect
- Health monitoring / basic logging

**Authentication & Access Control:**

```
Publisher Keys:   pub_<algo_id>_<random>
Subscriber Keys:  sub_<user_id>_<random>
```

Access mapping (stored in DynamoDB):
```json
{
  "subscribers": {
    "sub_alice_x8k2": ["algo_news_trader", "algo_momentum"],
    "sub_bob_m3j9": ["algo_news_trader"]
  }
}
```

- Alice receives signals from both algos, Bob only from news_trader
- You manage this mapping — add/remove access as needed

**Message Flow:**
1. Publisher authenticates → server validates publisher key
2. Subscriber authenticates → server validates subscriber key, loads allowed algos
3. Publisher sends signal → server looks up which subscribers have access → forwards to each

**Signal History:**
- Server stores last 24 hours of signals
- On client reconnect, server sends any signals the client missed (based on last-seen signal_id)
- Prevents missed trades due to brief network interruptions

**AWS Stack: API Gateway WebSocket + Lambda + DynamoDB**

Zero maintenance, scales to zero, well within free tier for this traffic (a few signals per day, handful of connections). DynamoDB stores connection state, access mappings, and signal history. Expect 200-500ms cold start latency on idle Lambdas — negligible given subscribers are already delayed by the post-execution flow.

### 3. Client App (subscriber runs this)

A standalone application subscribers run during market hours.

**Configuration** (`config.yaml`):
```yaml
relay_server: "wss://your-relay.example.com"
access_key: "sub_alice_x8k2"

alpaca:
  api_key: "their-alpaca-key"
  secret_key: "their-alpaca-secret"
  paper: false                    # true for paper trading

trading:
  position_size: 10000            # dollars per trade

eod:
  stop_new_positions_minutes: 20  # stop accepting signals N min before close
  close_all_minutes: 10           # close all positions N min before close

discord:
  bot_token: "their-discord-token"    # optional
  channel_id: "their-channel-id"      # optional
```

**Running:**
```bash
pip install trade-signal-relay
trade-relay-client --config config.yaml
```

**Signal Handling Flow:**
1. Receive signal from relay: `{"action": "open", "ticker": "AAPL", "side": "buy", "tp_percent": 5.0, "sl_percent": 1.5}`
2. Check if already in a position for this ticker → skip if yes
3. Get current price from Alpaca (`get_latest_trade`)
4. Calculate TP/SL prices from percentages:
   - BUY: `tp_price = price * (1 + tp_percent/100)`, `sl_price = price * (1 - sl_percent/100)`
   - SELL: `tp_price = price * (1 - tp_percent/100)`, `sl_price = price * (1 + sl_percent/100)`
5. Calculate shares: `int(position_size / price)`
6. Submit bracket order to Alpaca:
   ```python
   trading_api.submit_order(
       symbol=ticker,
       qty=shares,
       side=side,
       type='market',
       time_in_force='day',
       order_class='bracket',
       stop_loss={'stop_price': round(sl_price, 2)},
       take_profit={'limit_price': round(tp_price, 2)}
   )
   ```
7. Track position internally
8. Send Discord notification

**EOD Position Close Logic (runs independently, no signal from server):**

The client must replicate this logic from the live trader:
reference the live_real_money.py script in the news_trader repo.

```
Every check_market_hours() cycle (runs in main loop):
  1. Call trading_api.get_clock() to get market close time
  2. At close - 20 min: stop accepting new signals
  3. At close - 10 min: close all positions
     a. Cancel all open orders (bracket legs)
     b. Call close_all_positions()
     c. Wait 1 second, verify positions closed
     d. Retry until all closed or market closes
  4. After close: reset flags, wait for next market open
```

This is critical — the client cannot depend on the server for position closing. Network issues could leave subscribers stuck in positions overnight.

**Discord Notifications:**

Uses the same `SyncDiscordBot` pattern — runs discord.py in a background thread with a synchronous wrapper. Notifications for:
- Connected to relay / reconnected
- Signal received
- Position opened (ticker, shares, side, TP/SL prices)
- Position closed (by bracket order fill or EOD)
- Errors

Discord is optional — if not configured, client runs without it.

## Message Schema

All WebSocket messages are JSON with a `type` field:

```json
// Publisher → Server
{
  "type": "auth",
  "publisher_key": "pub_algo1_abc123"
}

{
  "type": "signal",
  "signal_id": "uuid",
  "action": "open",
  "ticker": "AAPL",
  "side": "buy",
  "tp_percent": 5.0,
  "sl_percent": 1.5,
  "timestamp": "2026-03-12T14:30:00Z"
}

// Server → Subscriber
{
  "type": "auth_result",
  "success": true
}

{
  "type": "signal",
  "signal_id": "uuid",
  "algo_id": "algo_news_trader",
  "action": "open",
  "ticker": "AAPL",
  "side": "buy",
  "tp_percent": 5.0,
  "sl_percent": 1.5,
  "timestamp": "2026-03-12T14:30:00Z"
}

// Subscriber → Server
{
  "type": "auth",
  "subscriber_key": "sub_alice_x8k2"
}

// Server → Publisher/Subscriber (connection management)
{
  "type": "error",
  "message": "Invalid key"
}

{
  "type": "ping"
}
```

## Publisher Integration Example

How you'd integrate the publisher into `live_real_money.py`:

```python
from relay_publisher import SignalPublisher

# In __init__:
self.signal_publisher = SignalPublisher(
    server_url="wss://your-relay.example.com",
    publisher_key=os.getenv("RELAY_PUBLISHER_KEY")
)
self.signal_publisher.connect()

# In open_position(), after the bracket order succeeds:
# tp_level and sl_level are already calculated
if direction == 'LONG':
    tp_pct = ((tp_level - current_price) / current_price) * 100
    sl_pct = ((current_price - sl_level) / current_price) * 100
else:
    tp_pct = ((current_price - tp_level) / current_price) * 100
    sl_pct = ((sl_level - current_price) / current_price) * 100

self.signal_publisher.publish_open(
    ticker=ticker,
    side='buy' if direction == 'LONG' else 'sell',
    tp_percent=tp_pct,
    sl_percent=sl_pct
)
```

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Signal timing | Post-execution | Simpler client, subscribers get confirmed trades |
| TP/SL format | Percentage-based | Accounts for subscriber price delay |
| EOD close | Client-side only | Can't rely on network for position safety |
| Transport | WebSocket | Real-time, simple, bidirectional for auth |
| Hosting | AWS API Gateway + Lambda | Zero maintenance, free tier sufficient |
| Access control | Per-algo subscriber keys | Granular — different people get different algos |
| Repo structure | Single repo, three packages | Shared schemas, simple versioning |

## Dependencies

**Server (Lambda):**
- `boto3` (DynamoDB for connection state, access mappings, signal history)

**Publisher:**
- `websockets`

**Client:**
- `websockets`
- `alpaca-trade-api`
- `discord.py` (optional)
- `pyyaml`
- `python-dotenv`
