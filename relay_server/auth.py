from datetime import datetime, timezone

from shared.auth import validate_publisher_key, validate_subscriber_key, extract_algo_id, extract_user_id


def validate_publisher(connection_id, key, connections_table):
    if not validate_publisher_key(key):
        return None
    algo_id = extract_algo_id(key)
    connections_table.put_item(Item={
        "connection_id": connection_id,
        "role": "publisher",
        "key": key,
        "algo_id": algo_id,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })
    return algo_id


def validate_subscriber(connection_id, key, connections_table, access_table):
    if not validate_subscriber_key(key):
        return None
    response = access_table.get_item(Key={"subscriber_key": key})
    if "Item" not in response:
        return None
    allowed_algos = response["Item"].get("allowed_algos", [])
    user_id = extract_user_id(key)
    connections_table.put_item(Item={
        "connection_id": connection_id,
        "role": "subscriber",
        "key": key,
        "user_id": user_id,
        "allowed_algos": allowed_algos,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })
    return allowed_algos


def get_subscribers_for_algo(algo_id, connections_table):
    response = connections_table.scan(
        FilterExpression="contains(allowed_algos, :algo_id) AND #r = :role",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={
            ":algo_id": algo_id,
            ":role": "subscriber",
        },
    )
    return [item["connection_id"] for item in response.get("Items", [])]
