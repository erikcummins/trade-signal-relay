All features must be unit tested. No feature is complete unless all tests pass.

Run tests: `python3 -m pytest tests/ -v`

## Project Overview

Trade Signal Relay: publishers send trade signals via WebSocket → AWS Lambda routes to subscribers → subscribers auto-execute bracket orders on Alpaca.

Four packages in one repo: `shared/` (message schemas, auth), `relay_publisher/` (algo library), `relay_server/` (Lambda), `relay_client/` (subscriber app).

## Reference Docs

- [docs/architecture.md](docs/architecture.md) — package layout, signal flow, key patterns (background thread + asyncio, message protocol, auth key format)
- [docs/testing.md](docs/testing.md) — how to run tests, mocking patterns (moto for DynamoDB, mock websockets, mock alpaca), e2e test setup
- [docs/server.md](docs/server.md) — Lambda handler, DynamoDB table schemas, auth flow, signal routing
- [docs/client.md](docs/client.md) — config loading, AlpacaTrader (TP/SL math, bracket orders), PositionManager (EOD close), Discord, main loop
- [docs/publisher.md](docs/publisher.md) — SignalPublisher integration, internals (queue, reconnect)
- [docs/deployment.md](docs/deployment.md) — deploy.sh commands, access management, teardown
