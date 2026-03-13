from dataclasses import dataclass
from typing import Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass
class AlpacaConfig:
    api_key: str
    secret_key: str
    paper: bool = False


@dataclass
class TradingConfig:
    position_size: int = 10000
    algo_sizes: dict = None

    def get_position_size(self, algo_id: str | None) -> int:
        if self.algo_sizes and algo_id and algo_id in self.algo_sizes:
            return self.algo_sizes[algo_id]
        return self.position_size


@dataclass
class EodConfig:
    stop_new_positions_minutes: int = 20
    close_all_minutes: int = 10


@dataclass
class DiscordConfig:
    bot_token: Optional[str] = None
    channel_id: Optional[str] = None


@dataclass
class Config:
    relay_server: str
    access_key: str
    alpaca: AlpacaConfig
    trading: TradingConfig
    eod: EodConfig
    discord: DiscordConfig


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError("Config file must be a YAML mapping")

    for key in ("relay_server", "access_key"):
        if not raw.get(key):
            raise ConfigError(f"Missing required field: {key}")

    alpaca_raw = raw.get("alpaca") or {}
    for key in ("api_key", "secret_key"):
        if not alpaca_raw.get(key):
            raise ConfigError(f"Missing required field: alpaca.{key}")

    alpaca = AlpacaConfig(
        api_key=alpaca_raw["api_key"],
        secret_key=alpaca_raw["secret_key"],
        paper=alpaca_raw.get("paper", False),
    )

    trading_raw = raw.get("trading") or {}
    algo_sizes = trading_raw.get("algo_sizes")
    if algo_sizes:
        algo_sizes = {k: int(v) for k, v in algo_sizes.items()}
    trading = TradingConfig(
        position_size=trading_raw.get("position_size", 10000),
        algo_sizes=algo_sizes,
    )

    eod_raw = raw.get("eod") or {}
    eod = EodConfig(
        stop_new_positions_minutes=eod_raw.get("stop_new_positions_minutes", 20),
        close_all_minutes=eod_raw.get("close_all_minutes", 10),
    )

    discord_raw = raw.get("discord") or {}
    discord = DiscordConfig(
        bot_token=discord_raw.get("bot_token"),
        channel_id=discord_raw.get("channel_id"),
    )

    return Config(
        relay_server=raw["relay_server"],
        access_key=raw["access_key"],
        alpaca=alpaca,
        trading=trading,
        eod=eod,
        discord=discord,
    )
