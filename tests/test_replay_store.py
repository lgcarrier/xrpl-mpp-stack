from __future__ import annotations

import asyncio

import pytest

from xrpl_mpp_facilitator.replay_store import RedisReplayStore
from tests.fakes import FakeRedis


def build_store() -> RedisReplayStore:
    return RedisReplayStore(
        FakeRedis(),
        processed_ttl_seconds=3600,
        pending_ttl_seconds=300,
    )


def test_replay_store_blocks_reused_invoice_ids_in_strict_mode() -> None:
    store = build_store()

    async def _run() -> None:
        reservation = await store.reserve("invoice-1", "blob-1")
        await store.mark_processed(reservation)

        with pytest.raises(ValueError, match="replay attack"):
            await store.reserve("invoice-1", "blob-2")

    asyncio.run(_run())


def test_replay_store_allows_new_blob_when_invoice_scope_is_disabled() -> None:
    store = build_store()

    async def _run() -> None:
        first = await store.reserve(None, "blob-1")
        await store.mark_processed(first)

        second = await store.reserve(None, "blob-2")
        await store.mark_processed(second)

        with pytest.raises(ValueError, match="replay attack"):
            await store.reserve(None, "blob-2")

    asyncio.run(_run())
