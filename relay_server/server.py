import json
import os
import time
import uuid

import boto3

from botocore.exceptions import ClientError

from shared.messages import serialize, deserialize, AuthResult, AuthSubscriber, Signal, Ping
from relay_server.auth import validate_publisher, validate_subscriber, get_subscribers_for_algo

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "relay-connections")
ACCESS_TABLE = os.environ.get("ACCESS_TABLE", "relay-access")
SIGNALS_TABLE = os.environ.get("SIGNALS_TABLE", "relay-signals")

TTL_SECONDS = 24 * 60 * 60


def _get_tables():
    dynamodb = boto3.resource("dynamodb")
    return (
        dynamodb.Table(CONNECTIONS_TABLE),
        dynamodb.Table(ACCESS_TABLE),
        dynamodb.Table(SIGNALS_TABLE),
    )


def _get_apigw_client(event):
    domain = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    endpoint = f"https://{domain}/{stage}"
    return boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)


def _post_to_connection(apigw, connection_id, data):
    try:
        apigw.post_to_connection(
            ConnectionId=connection_id,
            Data=data.encode("utf-8"),
        )
    except apigw.exceptions.GoneException:
        pass


def _handle_connect(connection_id, connections_table):
    connections_table.put_item(Item={
        "connection_id": connection_id,
        "role": "pending",
    })
    return {"statusCode": 200}


def _handle_disconnect(connection_id, connections_table):
    connections_table.delete_item(Key={"connection_id": connection_id})
    return {"statusCode": 200}


def _store_signal(signal, algo_id, signals_table):
    ttl = int(time.time()) + TTL_SECONDS
    sort_key = f"{signal.timestamp}#{signal.signal_id}"
    signals_table.put_item(
        Item={
            "algo_id": algo_id,
            "timestamp#signal_id": sort_key,
            "signal_id": signal.signal_id,
            "action": signal.action,
            "ticker": signal.ticker,
            "side": signal.side,
            "tp_percent": str(signal.tp_percent),
            "sl_percent": str(signal.sl_percent),
            "timestamp": signal.timestamp,
            "ttl": ttl,
        },
        ConditionExpression="attribute_not_exists(algo_id)",
    )


def _replay_missed_signals(apigw, connection_id, last_signal_id, allowed_algos, signals_table):
    found_marker = False
    signals_to_send = []

    for algo_id in allowed_algos:
        response = signals_table.query(
            KeyConditionExpression="algo_id = :aid",
            ExpressionAttributeValues={":aid": algo_id},
        )
        for item in response.get("Items", []):
            if not found_marker:
                if item["signal_id"] == last_signal_id:
                    found_marker = True
                continue
            signal = Signal(
                signal_id=item["signal_id"],
                action=item["action"],
                ticker=item["ticker"],
                side=item["side"],
                tp_percent=float(item["tp_percent"]),
                sl_percent=float(item["sl_percent"]),
                timestamp=item["timestamp"],
                algo_id=algo_id,
            )
            signals_to_send.append((item["timestamp#signal_id"], signal))

    signals_to_send.sort(key=lambda x: x[0])
    for _, signal in signals_to_send:
        _post_to_connection(apigw, connection_id, serialize(signal))


def _handle_auth(msg, connection_id, apigw, connections_table, access_table, signals_table):
    if isinstance(msg, AuthSubscriber):
        allowed_algos = validate_subscriber(connection_id, msg.subscriber_key, connections_table, access_table)
        success = allowed_algos is not None
        _post_to_connection(apigw, connection_id, serialize(AuthResult(success=success)))
        if success and msg.last_signal_id:
            _replay_missed_signals(apigw, connection_id, msg.last_signal_id, allowed_algos, signals_table)
    else:
        algo_id = validate_publisher(connection_id, msg.publisher_key, connections_table)
        success = algo_id is not None
        _post_to_connection(apigw, connection_id, serialize(AuthResult(success=success)))


def _handle_signal(signal, connection_id, apigw, connections_table, signals_table):
    response = connections_table.get_item(Key={"connection_id": connection_id})
    item = response.get("Item", {})
    if item.get("role") != "publisher":
        _post_to_connection(apigw, connection_id, serialize(AuthResult(success=False)))
        return

    algo_id = item["algo_id"]
    signal.algo_id = algo_id
    try:
        _store_signal(signal, algo_id, signals_table)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise

    subscriber_ids = get_subscribers_for_algo(algo_id, connections_table)
    signal_json = serialize(signal)
    for sub_id in subscriber_ids:
        _post_to_connection(apigw, sub_id, signal_json)


def _handle_default(event, connection_id, apigw, connections_table, access_table, signals_table):
    body = json.loads(event.get("body", "{}"))
    msg_type = body.get("type")

    if msg_type == "auth":
        from shared.messages import deserialize as _deser
        msg = _deser(event["body"])
        _handle_auth(msg, connection_id, apigw, connections_table, access_table, signals_table)
    elif msg_type == "signal":
        from shared.messages import deserialize as _deser
        signal = _deser(event["body"])
        _handle_signal(signal, connection_id, apigw, connections_table, signals_table)
    elif msg_type == "ping":
        _post_to_connection(apigw, connection_id, serialize(Ping()))
    else:
        _post_to_connection(apigw, connection_id, json.dumps({"type": "error", "message": f"Unknown type: {msg_type}"}))

    return {"statusCode": 200}


def handler(event, context):
    route_key = event["requestContext"]["routeKey"]
    connection_id = event["requestContext"]["connectionId"]
    connections_table, access_table, signals_table = _get_tables()

    if route_key == "$connect":
        return _handle_connect(connection_id, connections_table)
    elif route_key == "$disconnect":
        return _handle_disconnect(connection_id, connections_table)
    else:
        apigw = _get_apigw_client(event)
        return _handle_default(event, connection_id, apigw, connections_table, access_table, signals_table)
