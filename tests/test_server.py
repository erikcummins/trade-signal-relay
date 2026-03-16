import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import boto3
import pytest
from moto import mock_aws

from shared.messages import serialize, AuthPublisher, AuthSubscriber, AuthResult, Signal, Ping

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"


def _create_tables():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="relay-connections",
        KeySchema=[{"AttributeName": "connection_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connection_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName="relay-access",
        KeySchema=[{"AttributeName": "subscriber_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "subscriber_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName="relay-signals",
        KeySchema=[
            {"AttributeName": "algo_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp#signal_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "algo_id", "AttributeType": "S"},
            {"AttributeName": "timestamp#signal_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb


def _make_event(route_key, connection_id, body=None):
    event = {
        "requestContext": {
            "routeKey": route_key,
            "connectionId": connection_id,
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
        },
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def _mock_apigw():
    mock = MagicMock()
    mock.post_to_connection = MagicMock()
    mock.exceptions = MagicMock()
    mock.exceptions.GoneException = type("GoneException", (Exception,), {})
    return mock


class TestConnect:
    @mock_aws
    def test_connect_stores_connection(self):
        _create_tables()
        from relay_server.server import handler

        event = _make_event("$connect", "conn-1")
        result = handler(event, None)

        assert result == {"statusCode": 200}
        table = boto3.resource("dynamodb", region_name="us-east-1").Table("relay-connections")
        item = table.get_item(Key={"connection_id": "conn-1"})["Item"]
        assert item["connection_id"] == "conn-1"
        assert item["role"] == "pending"


class TestDisconnect:
    @mock_aws
    def test_disconnect_removes_connection(self):
        _create_tables()
        from relay_server.server import handler

        table = boto3.resource("dynamodb", region_name="us-east-1").Table("relay-connections")
        table.put_item(Item={"connection_id": "conn-1", "role": "publisher"})

        event = _make_event("$disconnect", "conn-1")
        result = handler(event, None)

        assert result == {"statusCode": 200}
        response = table.get_item(Key={"connection_id": "conn-1"})
        assert "Item" not in response


class TestPublisherAuth:
    @mock_aws
    def test_valid_key_stores_connection(self):
        _create_tables()
        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            body = serialize(AuthPublisher(publisher_key="pub_algo1_abc123"))
            event = _make_event("$default", "conn-1", body)
            handler(event, None)

        table = boto3.resource("dynamodb", region_name="us-east-1").Table("relay-connections")
        item = table.get_item(Key={"connection_id": "conn-1"})["Item"]
        assert item["role"] == "publisher"
        assert item["algo_id"] == "algo1"

        sent_data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(sent_data.decode("utf-8"))
        assert msg["type"] == "auth_result"
        assert msg["success"] is True

    @mock_aws
    def test_invalid_key_sends_failure(self):
        _create_tables()
        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            body = json.dumps({"type": "auth", "publisher_key": "bad_key"})
            event = _make_event("$default", "conn-1", body)
            handler(event, None)

        sent_data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(sent_data.decode("utf-8"))
        assert msg["type"] == "auth_result"
        assert msg["success"] is False


class TestSubscriberAuth:
    @mock_aws
    def test_valid_key_with_access(self):
        dynamodb = _create_tables()
        access_table = dynamodb.Table("relay-access")
        access_table.put_item(Item={
            "subscriber_key": "sub_user1_abc123",
            "allowed_algos": ["algo1", "algo2"],
        })
        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            body = serialize(AuthSubscriber(subscriber_key="sub_user1_abc123"))
            event = _make_event("$default", "conn-2", body)
            handler(event, None)

        table = dynamodb.Table("relay-connections")
        item = table.get_item(Key={"connection_id": "conn-2"})["Item"]
        assert item["role"] == "subscriber"
        assert item["user_id"] == "user1"
        assert item["allowed_algos"] == ["algo1", "algo2"]

        sent_data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(sent_data.decode("utf-8"))
        assert msg["success"] is True

    @mock_aws
    def test_key_not_in_access_table(self):
        _create_tables()
        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            body = serialize(AuthSubscriber(subscriber_key="sub_user1_abc123"))
            event = _make_event("$default", "conn-2", body)
            handler(event, None)

        sent_data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(sent_data.decode("utf-8"))
        assert msg["success"] is False


class TestSignalRouting:
    @mock_aws
    def test_signal_stored_and_fanned_out(self):
        dynamodb = _create_tables()
        conn_table = dynamodb.Table("relay-connections")
        access_table = dynamodb.Table("relay-access")

        conn_table.put_item(Item={
            "connection_id": "pub-conn",
            "role": "publisher",
            "key": "pub_algo1_abc123",
            "algo_id": "algo1",
        })
        conn_table.put_item(Item={
            "connection_id": "sub-conn-1",
            "role": "subscriber",
            "allowed_algos": ["algo1"],
        })
        conn_table.put_item(Item={
            "connection_id": "sub-conn-2",
            "role": "subscriber",
            "allowed_algos": ["algo2"],
        })

        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            signal = Signal(
                signal_id="sig-1", action="open", ticker="AAPL",
                side="buy", tp_percent=2.5, sl_percent=1.0,
                timestamp="2026-03-12T00:00:00Z",
            )
            body = serialize(signal)
            event = _make_event("$default", "pub-conn", body)
            handler(event, None)

        signals_table = dynamodb.Table("relay-signals")
        response = signals_table.scan()
        assert len(response["Items"]) == 1
        stored = response["Items"][0]
        assert stored["algo_id"] == "algo1"
        assert stored["signal_id"] == "sig-1"

        post_calls = mock_apigw.post_to_connection.call_args_list
        sent_connection_ids = [c[1]["ConnectionId"] for c in post_calls]
        assert "sub-conn-1" in sent_connection_ids
        assert "sub-conn-2" not in sent_connection_ids

        for c in post_calls:
            if c[1]["ConnectionId"] == "sub-conn-1":
                msg = json.loads(c[1]["Data"].decode("utf-8"))
                assert msg["type"] == "signal"
                assert msg["algo_id"] == "algo1"
                assert msg["ticker"] == "AAPL"

    @mock_aws
    def test_signal_from_unauthenticated_rejected(self):
        dynamodb = _create_tables()
        conn_table = dynamodb.Table("relay-connections")
        conn_table.put_item(Item={"connection_id": "conn-x", "role": "pending"})

        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            signal = Signal(
                signal_id="sig-1", action="open", ticker="AAPL",
                side="buy", tp_percent=2.5, sl_percent=1.0,
                timestamp="2026-03-12T00:00:00Z",
            )
            body = serialize(signal)
            event = _make_event("$default", "conn-x", body)
            handler(event, None)

        sent_data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(sent_data.decode("utf-8"))
        assert msg["success"] is False

        signals_table = dynamodb.Table("relay-signals")
        response = signals_table.scan()
        assert len(response["Items"]) == 0


class TestSignalDeduplication:
    @mock_aws
    def test_duplicate_signal_skips_fanout(self):
        dynamodb = _create_tables()
        conn_table = dynamodb.Table("relay-connections")

        conn_table.put_item(Item={
            "connection_id": "pub-conn",
            "role": "publisher",
            "key": "pub_algo1_abc123",
            "algo_id": "algo1",
        })
        conn_table.put_item(Item={
            "connection_id": "sub-conn-1",
            "role": "subscriber",
            "allowed_algos": ["algo1"],
        })

        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            signal = Signal(
                signal_id="sig-1", action="open", ticker="AAPL",
                side="buy", tp_percent=2.5, sl_percent=1.0,
                timestamp="2026-03-12T00:00:00Z",
            )
            body = serialize(signal)
            event = _make_event("$default", "pub-conn", body)
            handler(event, None)
            handler(event, None)

        post_calls = mock_apigw.post_to_connection.call_args_list
        sub_calls = [c for c in post_calls if c[1]["ConnectionId"] == "sub-conn-1"]
        assert len(sub_calls) == 1

        signals_table = dynamodb.Table("relay-signals")
        response = signals_table.scan()
        assert len(response["Items"]) == 1


class TestMissedSignalReplay:
    @mock_aws
    def test_replay_on_reconnect(self):
        dynamodb = _create_tables()
        access_table = dynamodb.Table("relay-access")
        access_table.put_item(Item={
            "subscriber_key": "sub_user1_abc123",
            "allowed_algos": ["algo1"],
        })

        signals_table = dynamodb.Table("relay-signals")
        signals_table.put_item(Item={
            "algo_id": "algo1",
            "timestamp#signal_id": "2026-03-12T00:00:00Z#sig-1",
            "signal_id": "sig-1",
            "action": "open", "ticker": "AAPL", "side": "buy",
            "tp_percent": "2.5", "sl_percent": "1.0",
            "timestamp": "2026-03-12T00:00:00Z",
            "ttl": 9999999999,
        })
        signals_table.put_item(Item={
            "algo_id": "algo1",
            "timestamp#signal_id": "2026-03-12T01:00:00Z#sig-2",
            "signal_id": "sig-2",
            "action": "open", "ticker": "TSLA", "side": "sell",
            "tp_percent": "3.0", "sl_percent": "1.5",
            "timestamp": "2026-03-12T01:00:00Z",
            "ttl": 9999999999,
        })
        signals_table.put_item(Item={
            "algo_id": "algo1",
            "timestamp#signal_id": "2026-03-12T02:00:00Z#sig-3",
            "signal_id": "sig-3",
            "action": "open", "ticker": "GOOG", "side": "buy",
            "tp_percent": "1.0", "sl_percent": "0.5",
            "timestamp": "2026-03-12T02:00:00Z",
            "ttl": 9999999999,
        })

        mock_apigw = _mock_apigw()

        with patch("relay_server.server._get_apigw_client", return_value=mock_apigw):
            from relay_server.server import handler

            body = json.dumps({
                "type": "auth",
                "subscriber_key": "sub_user1_abc123",
                "last_signal_id": "sig-1",
            })
            event = _make_event("$default", "sub-conn", body)
            handler(event, None)

        post_calls = mock_apigw.post_to_connection.call_args_list
        assert len(post_calls) == 3

        auth_msg = json.loads(post_calls[0][1]["Data"].decode("utf-8"))
        assert auth_msg["type"] == "auth_result"
        assert auth_msg["success"] is True

        replayed_1 = json.loads(post_calls[1][1]["Data"].decode("utf-8"))
        assert replayed_1["signal_id"] == "sig-2"
        assert replayed_1["ticker"] == "TSLA"

        replayed_2 = json.loads(post_calls[2][1]["Data"].decode("utf-8"))
        assert replayed_2["signal_id"] == "sig-3"
        assert replayed_2["ticker"] == "GOOG"
