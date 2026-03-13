# Relay Server

## Lambda Entry Point

`relay_server.server.handler(event, context)` — single function handles all WebSocket routes.

Routes by `event["requestContext"]["routeKey"]`:
- `$connect` → store connection as `role=pending`
- `$disconnect` → delete connection
- `$default` → parse body JSON, dispatch on `type` field

## DynamoDB Tables

| Table | PK | SK | Notes |
|-------|----|----|-------|
| `relay-connections` | `connection_id` (S) | — | role, key, algo_id/user_id, allowed_algos |
| `relay-access` | `subscriber_key` (S) | — | allowed_algos list |
| `relay-signals` | `algo_id` (S) | `timestamp#signal_id` (S) | TTL on `ttl` attribute, 24h |

Table names come from env vars with defaults: `CONNECTIONS_TABLE`, `ACCESS_TABLE`, `SIGNALS_TABLE`.

## Auth Flow

1. Publisher sends `{"type": "auth", "publisher_key": "pub_algo1_abc"}` → server validates key format, stores connection with `role=publisher` and `algo_id`
2. Subscriber sends `{"type": "auth", "subscriber_key": "sub_user1_xyz"}` → server validates key, looks up `relay-access` table for allowed_algos, stores connection with `role=subscriber`
3. Subscriber can include `last_signal_id` in auth → server replays missed signals from `relay-signals` table

## Signal Routing

1. Verify sender connection has `role=publisher`
2. Store signal in `relay-signals` with 24h TTL
3. Scan `relay-connections` for subscribers whose `allowed_algos` contains this `algo_id`
4. Send signal (with `algo_id` added) to each via `post_to_connection`

## Key Functions

- `relay_server.auth.validate_publisher(connection_id, key, table)` → algo_id or None
- `relay_server.auth.validate_subscriber(connection_id, key, conn_table, access_table)` → allowed_algos or None
- `relay_server.auth.get_subscribers_for_algo(algo_id, table)` → list of connection_ids
