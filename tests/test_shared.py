import json
import pytest

from shared.messages import (
    AuthPublisher, AuthSubscriber, AuthResult, Signal, Error, Ping,
    ValidationError, serialize, deserialize,
)
from shared.auth import (
    validate_publisher_key, validate_subscriber_key,
    extract_algo_id, extract_user_id,
)


class TestSerializationRoundTrip:
    def test_auth_publisher(self):
        msg = AuthPublisher(publisher_key="pub_algo1_abc123")
        result = deserialize(serialize(msg))
        assert isinstance(result, AuthPublisher)
        assert result.publisher_key == "pub_algo1_abc123"

    def test_auth_subscriber(self):
        msg = AuthSubscriber(subscriber_key="sub_user1_xyz789")
        result = deserialize(serialize(msg))
        assert isinstance(result, AuthSubscriber)
        assert result.subscriber_key == "sub_user1_xyz789"

    def test_auth_result(self):
        for val in (True, False):
            msg = AuthResult(success=val)
            result = deserialize(serialize(msg))
            assert isinstance(result, AuthResult)
            assert result.success is val

    def test_signal_without_algo_id(self):
        msg = Signal(
            signal_id="abc-123", action="open", ticker="AAPL",
            side="buy", tp_percent=2.5, sl_percent=1.0, timestamp="2026-01-01T00:00:00Z",
        )
        json_str = serialize(msg)
        assert "algo_id" not in json.loads(json_str)
        result = deserialize(json_str)
        assert isinstance(result, Signal)
        assert result.ticker == "AAPL"
        assert result.algo_id is None

    def test_signal_with_algo_id(self):
        msg = Signal(
            signal_id="abc-123", action="open", ticker="TSLA",
            side="sell", tp_percent=3.0, sl_percent=1.5, timestamp="2026-01-01T00:00:00Z",
            algo_id="algo1",
        )
        result = deserialize(serialize(msg))
        assert result.algo_id == "algo1"
        assert result.side == "sell"

    def test_error(self):
        msg = Error(message="something broke")
        result = deserialize(serialize(msg))
        assert isinstance(result, Error)
        assert result.message == "something broke"

    def test_ping(self):
        msg = Ping()
        result = deserialize(serialize(msg))
        assert isinstance(result, Ping)
        assert result.type == "ping"


class TestDeserializeDispatch:
    def test_dispatches_on_type(self):
        cases = [
            ('{"type":"auth","publisher_key":"k"}', AuthPublisher),
            ('{"type":"auth","subscriber_key":"k"}', AuthSubscriber),
            ('{"type":"auth_result","success":true}', AuthResult),
            ('{"type":"error","message":"x"}', Error),
            ('{"type":"ping"}', Ping),
        ]
        for json_str, expected_type in cases:
            assert isinstance(deserialize(json_str), expected_type)

    def test_unknown_type_raises(self):
        with pytest.raises(ValidationError, match="Unknown message type"):
            deserialize('{"type":"unknown"}')


class TestSignalValidation:
    def _signal_json(self, **overrides):
        base = {
            "type": "signal", "signal_id": "id1", "action": "open",
            "ticker": "AAPL", "side": "buy", "tp_percent": 2.0,
            "sl_percent": 1.0, "timestamp": "2026-01-01T00:00:00Z",
        }
        base.update(overrides)
        return json.dumps(base)

    def test_valid_signal(self):
        result = deserialize(self._signal_json())
        assert isinstance(result, Signal)

    def test_invalid_action(self):
        with pytest.raises(ValidationError, match="Invalid action"):
            deserialize(self._signal_json(action="close"))

    def test_invalid_side(self):
        with pytest.raises(ValidationError, match="Invalid side"):
            deserialize(self._signal_json(side="short"))

    def test_tp_percent_zero(self):
        with pytest.raises(ValidationError, match="tp_percent must be > 0"):
            deserialize(self._signal_json(tp_percent=0))

    def test_tp_percent_negative(self):
        with pytest.raises(ValidationError, match="tp_percent must be > 0"):
            deserialize(self._signal_json(tp_percent=-1))

    def test_sl_percent_zero(self):
        with pytest.raises(ValidationError, match="sl_percent must be > 0"):
            deserialize(self._signal_json(sl_percent=0))

    def test_sl_percent_negative(self):
        with pytest.raises(ValidationError, match="sl_percent must be > 0"):
            deserialize(self._signal_json(sl_percent=-1))


class TestKeyValidation:
    def test_valid_publisher_keys(self):
        assert validate_publisher_key("pub_algo1_abc123") is True
        assert validate_publisher_key("pub_myAlgo_x9z") is True

    def test_invalid_publisher_keys(self):
        assert validate_publisher_key("sub_user1_abc") is False
        assert validate_publisher_key("pub__abc") is False
        assert validate_publisher_key("pub_algo1") is False
        assert validate_publisher_key("") is False
        assert validate_publisher_key("pub_algo1_") is False

    def test_valid_subscriber_keys(self):
        assert validate_subscriber_key("sub_user1_abc123") is True
        assert validate_subscriber_key("sub_u2_xyz") is True

    def test_invalid_subscriber_keys(self):
        assert validate_subscriber_key("pub_algo1_abc") is False
        assert validate_subscriber_key("sub__abc") is False
        assert validate_subscriber_key("sub_user1") is False
        assert validate_subscriber_key("") is False


class TestIdExtraction:
    def test_extract_algo_id(self):
        assert extract_algo_id("pub_algo1_abc123") == "algo1"
        assert extract_algo_id("pub_myAlgo_x") == "myAlgo"

    def test_extract_algo_id_invalid(self):
        with pytest.raises(ValueError):
            extract_algo_id("sub_user1_abc")

    def test_extract_user_id(self):
        assert extract_user_id("sub_user1_abc123") == "user1"
        assert extract_user_id("sub_u2_xyz") == "u2"

    def test_extract_user_id_invalid(self):
        with pytest.raises(ValueError):
            extract_user_id("pub_algo1_abc")
