"""Shared test helpers: fully-typed Config construction for offline tests.

Network/Discord/clock fakes live in tests/fakes.py (US1 onward); this
module only builds Config values so Phase 2 tests need no environment.
"""

from __future__ import annotations

from botalert.config import Config


def make_config(
    discord_token: str = "test-discord-token",
    owner_discord_id: str = "111111111111111111",
    discord_manage_channel_id: str = "222222222222222222",
    webhook_url: str = "https://discord.com/api/webhooks/test",
    token: str = "test-shared-secret",
    port: int = 8080,
    base_url: str | None = None,
    state_file: str = "state.json",
    check_timeout_seconds: int = 15,
    check_pause_seconds: int = 2,
    failure_threshold: int = 10,
    default_cadence_minutes: float = 5.0,
    min_cadence_minutes: float = 1.0,
    max_cadence_minutes: float = 1440.0,
    user_agent: str = "botalert-test",
) -> Config:
    """Build a Config with deterministic test defaults."""
    return Config(
        discord_token=discord_token,
        owner_discord_id=owner_discord_id,
        discord_manage_channel_id=discord_manage_channel_id,
        webhook_url=webhook_url,
        token=token,
        port=port,
        base_url=base_url,
        state_file=state_file,
        check_timeout_seconds=check_timeout_seconds,
        check_pause_seconds=check_pause_seconds,
        failure_threshold=failure_threshold,
        default_cadence_minutes=default_cadence_minutes,
        min_cadence_minutes=min_cadence_minutes,
        max_cadence_minutes=max_cadence_minutes,
        user_agent=user_agent,
    )
