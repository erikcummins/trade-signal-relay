# Trade Signal Relay

A WebSocket relay that routes trade signals from algorithmic trading strategies to subscribers, who auto-execute bracket orders on Alpaca.

```
Algo → SignalPublisher → WebSocket → API Gateway
→ Lambda → store in DynamoDB → fan out to subscribers
→ RelayClient → AlpacaTrader → bracket order
```

## Packages

| Package | Description |
|---------|-------------|
| `shared/` | Message schemas and auth key validation (stdlib only) |
| `relay_publisher/` | Library for algos to publish signals via WebSocket |
| `relay_server/` | AWS Lambda handler — auth, signal storage, fan-out routing |
| `relay_client/` | Subscriber app — receives signals, executes trades on Alpaca |

## Quick Start

### Publisher (algo side)

```bash
pip install "trade-signal-relay[publisher] @ git+https://github.com/erikcummins/trade-signal-relay.git"
```

```python
from relay_publisher import SignalPublisher

publisher = SignalPublisher(
    server_url="wss://your-relay.execute-api.us-east-1.amazonaws.com/prod",
    publisher_key="pub_algo1_abc123"
)
publisher.connect()

publisher.publish_open(ticker="AAPL", side="buy", tp_percent=5.0, sl_percent=1.5)

publisher.disconnect()
```

### Subscriber (client side)

```bash
pip install "trade-signal-relay[client] @ git+https://github.com/erikcummins/trade-signal-relay.git"
cp config.example.yaml config.yaml  # edit with your keys
python -m relay_client --config config.yaml
```

### Server (deploy)

```bash
./infra/deploy.sh             # full deploy (idempotent)
./infra/deploy.sh update      # update Lambda code only
```

Manage access keys:

```bash
./infra/deploy.sh add-publisher myalgo                        # prints pub_myalgo_<random>
./infra/deploy.sh add-subscriber sub_alice_x8k2 algo1,algo2   # subscribe to algos
./infra/deploy.sh remove-subscriber sub_alice_x8k2
./infra/deploy.sh status                                       # show resources + connections
```

Teardown:

```bash
./infra/teardown.sh
```

## Client Config

See [`config.example.yaml`](config.example.yaml) for the full schema. Required fields: `relay_server`, `access_key`, `alpaca.api_key`, `alpaca.secret_key`.

## Tests

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

## Docs

- [Architecture](docs/architecture.md) — package layout, signal flow, key patterns
- [Server](docs/server.md) — Lambda handler, DynamoDB schemas, auth flow
- [Client](docs/client.md) — config, trading, position management, Discord notifications
- [Publisher](docs/publisher.md) — SignalPublisher integration and internals
- [Deployment](docs/deployment.md) — deploy/teardown commands, access management
- [Testing](docs/testing.md) — test patterns, mocking strategies, AWS integration tests
