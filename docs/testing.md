# Testing

## Running Tests

```bash
python3 -m pytest tests/ -v          # all tests
python3 -m pytest tests/test_e2e.py  # just e2e
```

pytest config is in `pyproject.toml` with `asyncio_mode = "strict"`.

## Test Patterns

### Server tests (`test_server.py`)
- Use `@mock_aws` from `moto` to mock DynamoDB
- `_create_tables()` helper creates all 3 DynamoDB tables
- `_make_event()` builds API Gateway WebSocket events
- `_mock_apigw()` mocks `apigatewaymanagementapi` client — check `post_to_connection.call_args_list` for sent messages
- Patch `relay_server.server._get_apigw_client` to inject mock

### Publisher tests (`test_publisher.py`)
- Test `_authenticate()` and `_connection_loop()` directly (async methods)
- Mark async tests with `@pytest.mark.asyncio`
- Mock `websockets.connect` with `MagicMock` context manager
- Mock `asyncio.sleep` to capture backoff timing

### Client tests (`test_client_connectivity.py`)
- Same async mock pattern as publisher tests
- Test `_authenticate()` and `_receive_loop()` directly
- Mock `websockets.connect` to verify auth message includes `last_signal_id`

### Trading tests (`test_client_trading.py`)
- Mock `alpaca_trade_api.REST` entirely
- For PositionManager: mock `api.get_clock()` returning an object with `is_open`, `next_close`, `timestamp`
- For AlpacaTrader: mock `api.get_latest_trade()` and `api.get_position()`
- Config tests: use `tmp_path` fixture to write YAML and test `load_config()`

### E2E test (`test_e2e.py`)
- `LocalRelay` class: real WebSocket server on a random port in a background thread
- Tests the full publisher → relay → subscriber flow without AWS
- Uses `threading.Event` with timeout to wait for signal receipt

### AWS integration tests (`test_smoke_aws.py`)
- Skipped by default (`@pytest.mark.skipif(not os.environ.get("RELAY_WS_URL"))`)
- Tests run against deployed AWS infrastructure (API Gateway + Lambda + DynamoDB)

**Setup:**
```bash
# Create test keys (after deploying with ./infra/deploy.sh)
./infra/deploy.sh add-publisher testalgo
./infra/deploy.sh add-publisher otheralgo
./infra/deploy.sh add-subscriber sub_tester_abc12345 testalgo
```

**Env vars** (add to `.env`, which is gitignored):
| Variable | Required | Description |
|---|---|---|
| `RELAY_WS_URL` | Yes | WebSocket endpoint from deploy output |
| `RELAY_PUBLISHER_KEY` | Yes | Key from `add-publisher testalgo` |
| `RELAY_SUBSCRIBER_KEY` | Yes | The subscriber key you provisioned |
| `RELAY_PUBLISHER_KEY_2` | No | Key from `add-publisher otheralgo` (for routing isolation test) |

The subscriber must be subscribed to the first publisher's algo but **not** the second.

**Running:**
```bash
source .env && python3 -m pytest tests/test_smoke_aws.py -v -s
```

**Test cases:**
- `test_publisher_connect_and_send` — publisher connects and sends a signal
- `test_subscriber_receives_signal` — signal reaches subscriber callback
- `test_auth_rejection` — unregistered subscriber key is rejected
- `test_signal_field_integrity` — all signal fields (ticker, side, action, tp/sl, algo_id, signal_id, timestamp) round-trip correctly
- `test_multiple_signals` — 3 signals all arrive (order not guaranteed by Lambda)
- `test_signal_routing_isolation` — signal from an unsubscribed algo does not arrive (skipped if `RELAY_PUBLISHER_KEY_2` not set)
- `test_latency` — measures and prints publish-to-callback round-trip time, asserts < 2s
