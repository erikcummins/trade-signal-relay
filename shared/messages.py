from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json
import re


@dataclass
class AuthPublisher:
    publisher_key: str
    type: str = field(default="auth", init=False)


@dataclass
class AuthSubscriber:
    subscriber_key: str
    last_signal_id: Optional[str] = None
    type: str = field(default="auth", init=False)


@dataclass
class AuthResult:
    success: bool
    type: str = field(default="auth_result", init=False)


@dataclass
class Signal:
    signal_id: str
    action: str
    ticker: str
    side: str
    tp_percent: Optional[float]
    sl_percent: float
    timestamp: str
    algo_id: Optional[str] = None
    eod_time: Optional[str] = None
    type: str = field(default="signal", init=False)


@dataclass
class Error:
    message: str
    type: str = field(default="error", init=False)


@dataclass
class Ping:
    type: str = field(default="ping", init=False)


class ValidationError(Exception):
    pass


_VALID_SIDES = {"buy", "sell"}
_VALID_ACTIONS = {"open"}
_EOD_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_signal(action: str, side: str, tp_percent, sl_percent: float, eod_time):
    if action not in _VALID_ACTIONS:
        raise ValidationError(f"Invalid action: {action}")
    if side not in _VALID_SIDES:
        raise ValidationError(f"Invalid side: {side}")
    if tp_percent is not None and tp_percent <= 0:
        raise ValidationError(f"tp_percent must be > 0 or null, got {tp_percent}")
    if sl_percent <= 0:
        raise ValidationError(f"sl_percent must be > 0, got {sl_percent}")
    if eod_time is not None and not _EOD_TIME_RE.match(eod_time):
        raise ValidationError(f"eod_time must be HH:MM (24h), got {eod_time}")


def serialize(msg) -> str:
    d = asdict(msg)
    if isinstance(msg, Signal):
        if msg.algo_id is None:
            del d["algo_id"]
        if msg.eod_time is None:
            del d["eod_time"]
    if isinstance(msg, AuthSubscriber) and msg.last_signal_id is None:
        del d["last_signal_id"]
    return json.dumps(d)


def _parse_tp(value):
    if value is None:
        return None
    return float(value)


def _parse_auth(data: dict):
    if "publisher_key" in data:
        return AuthPublisher(publisher_key=data["publisher_key"])
    if "subscriber_key" in data:
        return AuthSubscriber(
            subscriber_key=data["subscriber_key"],
            last_signal_id=data.get("last_signal_id"),
        )
    raise ValidationError("Auth message must contain publisher_key or subscriber_key")


def _parse_signal(data: dict) -> Signal:
    try:
        tp = _parse_tp(data.get("tp_percent"))
        sl = float(data["sl_percent"])
    except (KeyError, ValueError, TypeError) as e:
        raise ValidationError(str(e))
    eod_time = data.get("eod_time")
    _validate_signal(data.get("action", ""), data.get("side", ""), tp, sl, eod_time)
    return Signal(
        signal_id=data["signal_id"],
        action=data["action"],
        ticker=data["ticker"],
        side=data["side"],
        tp_percent=tp,
        sl_percent=sl,
        timestamp=data["timestamp"],
        algo_id=data.get("algo_id"),
        eod_time=eod_time,
    )


def deserialize(json_str: str):
    data = json.loads(json_str)
    msg_type = data.get("type")
    parsers = {
        "auth": _parse_auth,
        "auth_result": lambda d: AuthResult(success=d["success"]),
        "signal": _parse_signal,
        "error": lambda d: Error(message=d["message"]),
        "ping": lambda d: Ping(),
    }
    if msg_type not in parsers:
        raise ValidationError(f"Unknown message type: {msg_type}")
    return parsers[msg_type](data)
