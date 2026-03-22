from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.fakes import FakeRedis
from xrpl_mpp_facilitator.session_store import RedisSessionStore, SessionState


def build_store(redis_client: FakeRedis | None = None) -> RedisSessionStore:
    return RedisSessionStore(
        redis_client or FakeRedis(),
        session_ttl_seconds=3600,
        idle_timeout_seconds=900,
    )


def test_assert_open_uses_session_specific_idle_timeout() -> None:
    store = build_store()
    last_activity = (datetime.now(UTC) - timedelta(seconds=61)).isoformat().replace("+00:00", "Z")
    state = SessionState(
        session_id="session-123",
        session_token_hash="hashed-token",
        status="open",
        payer="rBuyer",
        recipient="rMerchant",
        asset_identifier="XRP:native",
        network="xrpl:1",
        unit_amount="250",
        min_prepay_amount="1000",
        idle_timeout_seconds=60,
        prepaid_total="1000",
        spent_total="250",
        available_balance="750",
        last_activity_at=last_activity,
    )

    with pytest.raises(ValueError, match="Session timed out"):
        store._assert_open(state)


def test_consume_rejects_session_challenge_binding_mismatch() -> None:
    redis_client = FakeRedis()
    store = build_store(redis_client)

    async def _run() -> None:
        await store.begin_open_session(
            session_id="session-123",
            session_token="session-token",
            payer="rBuyer",
            recipient="rMerchant",
            asset_identifier="XRP:native",
            network="xrpl:1",
            unit_amount="250",
            min_prepay_amount="1000",
            idle_timeout_seconds=60,
            prepaid_total=Decimal("1000"),
            initial_spend=Decimal("250"),
            action_id="open-action",
        )
        await store.commit_open_session(
            session_id="session-123",
            action_id="open-action",
            initial_spend=Decimal("250"),
        )
        with pytest.raises(
            ValueError,
            match="Session unit amount does not match the payment challenge",
        ):
            await store.consume(
                session_id="session-123",
                session_token="session-token",
                recipient="rMerchant",
                asset_identifier="XRP:native",
                network="xrpl:1",
                unit_amount="500",
                min_prepay_amount="1000",
                amount=Decimal("500"),
                action_id="use-action",
            )

    asyncio.run(_run())
