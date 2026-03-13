# Relay Publisher

## Integration

```python
from relay_publisher import SignalPublisher

publisher = SignalPublisher(
    server_url="wss://your-relay.execute-api.us-east-1.amazonaws.com/prod",
    publisher_key="pub_algo1_abc123"  # validated on construction, raises ValueError
)
publisher.connect()  # blocks until authenticated

publisher.publish_open(
    ticker="AAPL",
    side="buy",       # "buy" or "sell"
    tp_percent=5.0,
    sl_percent=1.5
)
# Non-blocking, fire-and-forget. Signal gets UUID + UTC timestamp automatically.

publisher.disconnect()  # clean shutdown
```

## Internals (`relay_publisher/publisher.py`)

- Background daemon thread runs asyncio event loop
- `queue.Queue` for thread-safe message passing from `publish_open()` to send loop
- `_connection_loop`: auto-reconnect with exponential backoff (1s, 2s, 4s... cap 30s), resets on success
- `_authenticate`: sends `AuthPublisher`, waits for `AuthResult`, raises `ConnectionError` on failure
- `_send_loop`: polls queue with 0.1s timeout, sends serialized messages, stops on None sentinel
