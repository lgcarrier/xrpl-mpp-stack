from __future__ import annotations

import asyncio

from xrpl_mpp_facilitator.replay_store import (
    InMemoryChallengeReplayStore,
    RedisReplayStore,
)
from tests.fakes import FakeRedis


def test_in_memory_challenge_claim_has_one_winner_and_honors_retention() -> None:
    now = [100.0]
    store = InMemoryChallengeReplayStore(clock=lambda: now[0])

    async def _run() -> None:
        results = await asyncio.gather(
            *(
                store.claim_challenge("testnet\x00challenge-1", retention_seconds=60)
                for _ in range(20)
            )
        )
        assert results.count(True) == 1
        assert results.count(False) == 19

        now[0] += 61
        assert await store.claim_challenge(
            "testnet\x00challenge-1",
            retention_seconds=60,
        )

        assert await store.claim_challenge(
            "testnet\x00challenge-without-expiry",
            retention_seconds=None,
        )
        now[0] += 10_000
        assert not await store.claim_challenge(
            "testnet\x00challenge-without-expiry",
            retention_seconds=None,
        )

    asyncio.run(_run())


def test_redis_challenge_claim_has_one_winner_and_fail_closed_retention() -> None:
    redis = FakeRedis()
    store = RedisReplayStore(
        redis,
        processed_ttl_seconds=1,
        pending_ttl_seconds=1,
    )

    async def _run() -> None:
        results = await asyncio.gather(
            *(
                store.claim_challenge("testnet\x00challenge-2", retention_seconds=60)
                for _ in range(20)
            )
        )
        assert results.count(True) == 1
        assert results.count(False) == 19

        # PayChannel challenge retention is independent from the store's short
        # generic replay TTL and cannot reopen while the challenge is valid.
        redis.advance(2)
        assert not await store.claim_challenge(
            "testnet\x00challenge-2",
            retention_seconds=60,
        )

        redis.advance(59)
        assert await store.claim_challenge(
            "testnet\x00challenge-2",
            retention_seconds=60,
        )

        assert await store.claim_challenge(
            "testnet\x00challenge-without-expiry",
            retention_seconds=None,
        )
        redis.advance(100_000)
        assert not await store.claim_challenge(
            "testnet\x00challenge-without-expiry",
            retention_seconds=None,
        )

    asyncio.run(_run())
