"""Typed configuration loaded from the environment (data-model.md §5).

The five required variables fail fast at startup with a named-variable
error when missing or empty — no silent defaults for secrets or ids.
Config is env-only and never persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from botalert.fetcher import BACKENDS

_DEFAULT_USER_AGENT = "botalert/0.1 (personal stock checker)"

# Order here is the order of the startup error message (first missing wins).
_REQUIRED_VARS: tuple[str, ...] = (
    "DISCORD_TOKEN",
    "OWNER_DISCORD_ID",
    "DISCORD_MANAGE_CHANNEL_ID",
    "BOTALERT_WEBHOOK_URL",
    "BOTALERT_TOKEN",
)


class ConfigError(Exception):
    """Configuration is missing or invalid; startup must abort."""


@dataclass(frozen=True)
class Config:
    """All runtime configuration. Env-only — never persisted."""

    discord_token: str
    owner_discord_id: str
    discord_manage_channel_id: str
    webhook_url: str
    token: str
    port: int
    base_url: str | None
    state_file: str
    check_timeout_seconds: int
    check_pause_seconds: int
    failure_threshold: int
    default_cadence_minutes: float
    min_cadence_minutes: float
    max_cadence_minutes: float
    user_agent: str
    fetch_backend: str


def _required(name: str) -> str:
    """Read a required variable, failing fast with its name."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(f"missing required environment variable: {name}")
    return value.strip()


def _int(name: str, default: int) -> int:
    """Read an optional integer variable with a default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}") from None


def _choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    """Read an optional enum variable, failing fast on an unknown value."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    if raw not in choices:
        raise ConfigError(
            f"{name} must be one of: {'|'.join(choices)}, got: {raw!r}"
        )
    return raw


def _float(name: str, default: float) -> float:
    """Read an optional numeric variable with a default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got: {raw!r}") from None


def load_config() -> Config:
    """Load configuration from the environment (.env in local dev only).

    Returns a typed Config. Raises ConfigError naming the first
    missing/invalid variable (fail fast, no silent defaults).
    """
    load_dotenv()
    config = Config(
        discord_token=_required("DISCORD_TOKEN"),
        owner_discord_id=_required("OWNER_DISCORD_ID"),
        discord_manage_channel_id=_required("DISCORD_MANAGE_CHANNEL_ID"),
        webhook_url=_required("BOTALERT_WEBHOOK_URL"),
        token=_required("BOTALERT_TOKEN"),
        port=_int("PORT", 8080),
        base_url=os.getenv("BOTALERT_BASE_URL") or None,
        state_file=os.getenv("STATE_FILE") or "state.json",
        check_timeout_seconds=_int("CHECK_TIMEOUT_SECONDS", 15),
        check_pause_seconds=_int("CHECK_PAUSE_SECONDS", 2),
        failure_threshold=_int("FAILURE_THRESHOLD", 10),
        default_cadence_minutes=_float("DEFAULT_CADENCE_MINUTES", 5.0),
        min_cadence_minutes=_float("MIN_CADENCE_MINUTES", 1.0),
        max_cadence_minutes=_float("MAX_CADENCE_MINUTES", 1440.0),
        user_agent=os.getenv("USER_AGENT") or _DEFAULT_USER_AGENT,
        fetch_backend=_choice("FETCH_BACKEND", "auto", BACKENDS),
    )
    if not 1 <= config.port <= 65535:
        raise ConfigError(f"PORT must be between 1 and 65535, got: {config.port}")
    if config.min_cadence_minutes > config.max_cadence_minutes:
        raise ConfigError("MIN_CADENCE_MINUTES must be <= MAX_CADENCE_MINUTES")
    return config


__all__ = [
    "Config",
    "ConfigError",
    "_REQUIRED_VARS",
    "load_config",
]
