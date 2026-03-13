import re

_PUBLISHER_RE = re.compile(r"^pub_([a-zA-Z0-9]+)_[a-zA-Z0-9]+$")
_SUBSCRIBER_RE = re.compile(r"^sub_([a-zA-Z0-9]+)_[a-zA-Z0-9]+$")


def validate_publisher_key(key: str) -> bool:
    return _PUBLISHER_RE.match(key) is not None


def validate_subscriber_key(key: str) -> bool:
    return _SUBSCRIBER_RE.match(key) is not None


def extract_algo_id(publisher_key: str) -> str:
    m = _PUBLISHER_RE.match(publisher_key)
    if not m:
        raise ValueError(f"Invalid publisher key: {publisher_key}")
    return m.group(1)


def extract_user_id(subscriber_key: str) -> str:
    m = _SUBSCRIBER_RE.match(subscriber_key)
    if not m:
        raise ValueError(f"Invalid subscriber key: {subscriber_key}")
    return m.group(1)
