from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.replay_store import (
    REPLAY_RETENTION_CLOCK_SKEW_SECONDS,
    RedisReplayStore,
    build_replay_store,
    replay_retention_seconds,
)
from tests.fakes import FakeRedis


def build_store() -> RedisReplayStore:
    return RedisReplayStore(
        FakeRedis(),
        processed_ttl_seconds=3600,
        pending_ttl_seconds=300,
        network="testnet",
    )


def test_replay_store_blocks_reused_invoice_ids_in_strict_mode() -> None:
    store = build_store()

    async def _run() -> None:
        reservation = await store.reserve(
            "invoice-1",
            "blob-1",
            retention_seconds=120,
        )
        await store.mark_processed(reservation)

        with pytest.raises(ValueError, match="replay attack"):
            await store.reserve(
                "invoice-1",
                "blob-2",
                retention_seconds=120,
            )

    asyncio.run(_run())


def test_replay_store_allows_new_blob_when_invoice_scope_is_disabled() -> None:
    store = build_store()

    async def _run() -> None:
        first = await store.reserve(None, "blob-1", retention_seconds=120)
        await store.mark_processed(first)

        second = await store.reserve(None, "blob-2", retention_seconds=120)
        await store.mark_processed(second)

        with pytest.raises(ValueError, match="replay attack"):
            await store.reserve(None, "blob-2", retention_seconds=120)

    asyncio.run(_run())


def test_replay_store_isolates_identical_charge_references_by_network() -> None:
    redis = FakeRedis()
    common_settings = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": "rTESTDESTINATIONADDRESS123456789",
        "REDIS_URL": "redis://redis:6379/0",
        "FACILITATOR_BEARER_TOKEN": "test-token",
        "MPP_CHALLENGE_SECRET": "test-challenge-secret",
    }
    mainnet = build_replay_store(
        Settings(NETWORK_ID="mainnet", **common_settings),
        redis,
    )
    testnet = build_replay_store(
        Settings(NETWORK_ID="testnet", **common_settings),
        redis,
    )

    async def _run() -> None:
        mainnet_reservation = await mainnet.reserve(
            "shared-invoice",
            "shared-blob-hash",
            retention_seconds=120,
        )
        await mainnet.mark_processed(mainnet_reservation)

        testnet_reservation = await testnet.reserve(
            "shared-invoice",
            "shared-blob-hash",
            retention_seconds=120,
        )
        await testnet.mark_processed(testnet_reservation)

        with pytest.raises(ValueError, match="replay attack"):
            await mainnet.reserve(
                "shared-invoice",
                "shared-blob-hash",
                retention_seconds=120,
            )
        with pytest.raises(ValueError, match="replay attack"):
            await testnet.reserve(
                "shared-invoice",
                "shared-blob-hash",
                retention_seconds=120,
            )

    asyncio.run(_run())

    assert redis.get_string(
        "facilitator:replay:mainnet:invoice:shared-invoice"
    ) == "processed"
    assert redis.get_string(
        "facilitator:replay:testnet:invoice:shared-invoice"
    ) == "processed"
    assert redis.get_string(
        "facilitator:replay:mainnet:blob:shared-blob-hash"
    ) == "processed"
    assert redis.get_string(
        "facilitator:replay:testnet:blob:shared-blob-hash"
    ) == "processed"


def test_expiry_derived_retention_overrides_short_static_replay_ttls() -> None:
    redis = FakeRedis()
    store = RedisReplayStore(
        redis,
        processed_ttl_seconds=1,
        pending_ttl_seconds=1,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    remaining_seconds = 5
    validation_timeout_seconds = 2
    retention = replay_retention_seconds(
        (now + timedelta(seconds=remaining_seconds)).isoformat(),
        validation_timeout_seconds=validation_timeout_seconds,
        now=now,
    )

    assert retention == (
        remaining_seconds
        + validation_timeout_seconds
        + REPLAY_RETENTION_CLOCK_SKEW_SECONDS
    )

    async def _run() -> None:
        reservation = await store.reserve(
            "invoice-short-static-ttl",
            "blob-short-static-ttl",
            retention_seconds=retention,
        )
        await store.mark_processed(reservation)

        redis.advance(2)
        with pytest.raises(ValueError, match="replay attack"):
            await store.reserve(
                "invoice-short-static-ttl",
                "blob-short-static-ttl",
                retention_seconds=retention,
            )

        redis.advance(retention - 2)
        await store.reserve(
            "invoice-short-static-ttl",
            "blob-short-static-ttl",
            retention_seconds=retention,
        )

    asyncio.run(_run())


@pytest.mark.parametrize(
    "expires_iso",
    [None, "not-a-date", "2026-01-01T00:05:00"],
)
def test_replay_retention_fails_closed_without_usable_authenticated_expiry(
    expires_iso: str | None,
) -> None:
    with pytest.raises(ValueError, match="expires"):
        replay_retention_seconds(
            expires_iso,
            validation_timeout_seconds=15,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
