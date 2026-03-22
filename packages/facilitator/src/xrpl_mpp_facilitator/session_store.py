from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol

from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.redis_utils import create_async_redis_client


@dataclass(frozen=True)
class SessionState:
    session_id: str
    session_token_hash: str
    status: str
    payer: str
    recipient: str
    asset_identifier: str
    network: str
    unit_amount: str
    min_prepay_amount: str
    idle_timeout_seconds: int
    prepaid_total: str
    spent_total: str
    available_balance: str
    last_activity_at: str
    pending_action_id: str | None = None
    pending_top_up_amount: str | None = None

    def prepaid_decimal(self) -> Decimal:
        return Decimal(self.prepaid_total)

    def spent_decimal(self) -> Decimal:
        return Decimal(self.spent_total)

    def available_decimal(self) -> Decimal:
        return Decimal(self.available_balance)


class SessionStore(Protocol):
    async def begin_open_session(
        self,
        *,
        session_id: str,
        session_token: str,
        payer: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        idle_timeout_seconds: int,
        prepaid_total: Decimal,
        initial_spend: Decimal,
        action_id: str,
    ) -> SessionState:
        ...

    async def commit_open_session(
        self,
        *,
        session_id: str,
        action_id: str,
        initial_spend: Decimal,
    ) -> SessionState:
        ...

    async def abort_open_session(
        self,
        *,
        session_id: str,
        action_id: str,
    ) -> None:
        ...

    async def consume(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        amount: Decimal,
        action_id: str,
    ) -> SessionState:
        ...

    async def begin_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        amount: Decimal,
        action_id: str,
    ) -> SessionState:
        ...

    async def commit_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        action_id: str,
    ) -> SessionState:
        ...

    async def abort_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        action_id: str,
    ) -> None:
        ...

    async def close_session(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        action_id: str,
    ) -> SessionState:
        ...

    async def get(self, session_id: str) -> SessionState | None:
        ...


class RedisSessionStore:
    def __init__(
        self,
        redis_client: Any,
        *,
        session_ttl_seconds: int,
        idle_timeout_seconds: int,
    ) -> None:
        self._redis = redis_client
        self._session_ttl_seconds = session_ttl_seconds
        self._default_idle_timeout_seconds = idle_timeout_seconds

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"facilitator:session:{session_id}"

    @staticmethod
    def _action_key(session_id: str, action_id: str) -> str:
        return f"facilitator:session:{session_id}:action:{action_id}"

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _serialize(cls, state: SessionState) -> str:
        return json.dumps(
            {
                "session_id": state.session_id,
                "session_token_hash": state.session_token_hash,
                "status": state.status,
                "payer": state.payer,
                "recipient": state.recipient,
                "asset_identifier": state.asset_identifier,
                "network": state.network,
                "unit_amount": state.unit_amount,
                "min_prepay_amount": state.min_prepay_amount,
                "idle_timeout_seconds": state.idle_timeout_seconds,
                "prepaid_total": state.prepaid_total,
                "spent_total": state.spent_total,
                "available_balance": state.available_balance,
                "last_activity_at": state.last_activity_at,
                "pending_action_id": state.pending_action_id,
                "pending_top_up_amount": state.pending_top_up_amount,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _deserialize(raw_state: str | bytes | None) -> SessionState | None:
        if raw_state is None:
            return None
        if isinstance(raw_state, bytes):
            raw_state = raw_state.decode("utf-8")
        payload = json.loads(raw_state)
        return SessionState(**payload)

    def _assert_open(self, state: SessionState) -> None:
        if state.status != "open":
            raise ValueError("Session is not active")
        if state.pending_action_id is not None:
            raise ValueError("Session update already in progress")
        last_activity = datetime.fromisoformat(state.last_activity_at.replace("Z", "+00:00"))
        deadline = last_activity + timedelta(seconds=state.idle_timeout_seconds)
        if deadline <= datetime.now(UTC):
            raise ValueError("Session timed out")

    def _assert_token(self, state: SessionState, session_token: str) -> None:
        if self._hash_token(session_token) != state.session_token_hash:
            raise ValueError("Session token invalid")

    @staticmethod
    def _assert_binding_matches(
        state: SessionState,
        *,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
    ) -> None:
        if state.recipient != recipient:
            raise ValueError("Session recipient does not match the payment challenge")
        if state.asset_identifier != asset_identifier:
            raise ValueError("Session asset does not match the payment challenge")
        if state.network != network:
            raise ValueError("Session network does not match the payment challenge")
        if state.unit_amount != unit_amount:
            raise ValueError("Session unit amount does not match the payment challenge")
        if state.min_prepay_amount != min_prepay_amount:
            raise ValueError("Session minimum prepay amount does not match the payment challenge")

    async def begin_open_session(
        self,
        *,
        session_id: str,
        session_token: str,
        payer: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        idle_timeout_seconds: int,
        prepaid_total: Decimal,
        initial_spend: Decimal,
        action_id: str,
    ) -> SessionState:
        if prepaid_total < initial_spend:
            raise ValueError("Session prepay amount is below the required spend")

        state = SessionState(
            session_id=session_id,
            session_token_hash=self._hash_token(session_token),
            status="pending_open",
            payer=payer,
            recipient=recipient,
            asset_identifier=asset_identifier,
            network=network,
            unit_amount=unit_amount,
            min_prepay_amount=min_prepay_amount,
            idle_timeout_seconds=idle_timeout_seconds or self._default_idle_timeout_seconds,
            prepaid_total=str(prepaid_total),
            spent_total="0",
            available_balance=str(prepaid_total),
            last_activity_at=self._now_iso(),
            pending_action_id=action_id,
        )
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)

        async with self._redis.pipeline() as pipe:
            await pipe.watch(session_key, action_key)
            existing_session, existing_action = await pipe.mget(session_key, action_key)
            if existing_session is not None:
                raise ValueError("Session already exists")
            if existing_action is not None:
                raise ValueError("Session action already processed")
            pipe.multi()
            pipe.set(session_key, self._serialize(state), ex=self._session_ttl_seconds)
            pipe.set(action_key, "1", ex=self._session_ttl_seconds)
            await pipe.execute()
        return state

    async def commit_open_session(
        self,
        *,
        session_id: str,
        action_id: str,
        initial_spend: Decimal,
    ) -> SessionState:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, existing_action = await pipe.mget(session_key, action_key)
                    if raw_state is None:
                        raise ValueError("Session not found")
                    state = self._deserialize(raw_state)
                    if state is None:
                        raise ValueError("Session state invalid")
                    if state.status != "pending_open" or state.pending_action_id != action_id:
                        raise ValueError("Session open is not pending")
                    if state.prepaid_decimal() < initial_spend:
                        raise ValueError("Session prepay amount is below the required spend")
                    updated = SessionState(
                        session_id=state.session_id,
                        session_token_hash=state.session_token_hash,
                        status="open",
                        payer=state.payer,
                        recipient=state.recipient,
                        asset_identifier=state.asset_identifier,
                        network=state.network,
                        unit_amount=state.unit_amount,
                        min_prepay_amount=state.min_prepay_amount,
                        idle_timeout_seconds=state.idle_timeout_seconds,
                        prepaid_total=state.prepaid_total,
                        spent_total=str(initial_spend),
                        available_balance=str(state.prepaid_decimal() - initial_spend),
                        last_activity_at=self._now_iso(),
                    )
                    pipe.multi()
                    pipe.set(session_key, self._serialize(updated), ex=self._session_ttl_seconds)
                    pipe.set(action_key, "1", ex=self._session_ttl_seconds)
                    await pipe.execute()
                    return updated
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def abort_open_session(
        self,
        *,
        session_id: str,
        action_id: str,
    ) -> None:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, existing_action = await pipe.mget(session_key, action_key)
                    state = self._deserialize(raw_state)
                    pipe.multi()
                    if state is not None and state.status == "pending_open" and state.pending_action_id == action_id:
                        pipe.delete(session_key)
                    if existing_action is not None:
                        pipe.delete(action_key)
                    await pipe.execute()
                    return
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def consume(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        amount: Decimal,
        action_id: str,
    ) -> SessionState:
        return await self._mutate(
            session_id=session_id,
            session_token=session_token,
            recipient=recipient,
            asset_identifier=asset_identifier,
            network=network,
            unit_amount=unit_amount,
            min_prepay_amount=min_prepay_amount,
            action_id=action_id,
            mutate=lambda state: self._consume_state(state, amount),
        )

    async def begin_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        amount: Decimal,
        action_id: str,
    ) -> SessionState:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, existing_action = await pipe.mget(session_key, action_key)
                    if raw_state is None:
                        raise ValueError("Session not found")
                    if existing_action is not None:
                        raise ValueError("Session action already processed")
                    state = self._deserialize(raw_state)
                    if state is None:
                        raise ValueError("Session state invalid")
                    self._assert_token(state, session_token)
                    self._assert_open(state)
                    self._assert_binding_matches(
                        state,
                        recipient=recipient,
                        asset_identifier=asset_identifier,
                        network=network,
                        unit_amount=unit_amount,
                        min_prepay_amount=min_prepay_amount,
                    )
                    if amount <= Decimal("0"):
                        raise ValueError("Session top-up amount must be greater than zero")
                    updated = SessionState(
                        session_id=state.session_id,
                        session_token_hash=state.session_token_hash,
                        status=state.status,
                        payer=state.payer,
                        recipient=state.recipient,
                        asset_identifier=state.asset_identifier,
                        network=state.network,
                        unit_amount=state.unit_amount,
                        min_prepay_amount=state.min_prepay_amount,
                        idle_timeout_seconds=state.idle_timeout_seconds,
                        prepaid_total=state.prepaid_total,
                        spent_total=state.spent_total,
                        available_balance=state.available_balance,
                        last_activity_at=self._now_iso(),
                        pending_action_id=action_id,
                        pending_top_up_amount=str(amount),
                    )
                    pipe.multi()
                    pipe.set(session_key, self._serialize(updated), ex=self._session_ttl_seconds)
                    pipe.set(action_key, "1", ex=self._session_ttl_seconds)
                    await pipe.execute()
                    return updated
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def commit_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        action_id: str,
    ) -> SessionState:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, _existing_action = await pipe.mget(session_key, action_key)
                    if raw_state is None:
                        raise ValueError("Session not found")
                    state = self._deserialize(raw_state)
                    if state is None:
                        raise ValueError("Session state invalid")
                    self._assert_token(state, session_token)
                    if state.pending_action_id != action_id or state.pending_top_up_amount is None:
                        raise ValueError("Session top-up is not pending")
                    pending_amount = Decimal(state.pending_top_up_amount)
                    updated = SessionState(
                        session_id=state.session_id,
                        session_token_hash=state.session_token_hash,
                        status=state.status,
                        payer=state.payer,
                        recipient=state.recipient,
                        asset_identifier=state.asset_identifier,
                        network=state.network,
                        unit_amount=state.unit_amount,
                        min_prepay_amount=state.min_prepay_amount,
                        idle_timeout_seconds=state.idle_timeout_seconds,
                        prepaid_total=str(state.prepaid_decimal() + pending_amount),
                        spent_total=state.spent_total,
                        available_balance=str(state.available_decimal() + pending_amount),
                        last_activity_at=self._now_iso(),
                    )
                    pipe.multi()
                    pipe.set(session_key, self._serialize(updated), ex=self._session_ttl_seconds)
                    pipe.set(action_key, "1", ex=self._session_ttl_seconds)
                    await pipe.execute()
                    return updated
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def abort_top_up(
        self,
        *,
        session_id: str,
        session_token: str,
        action_id: str,
    ) -> None:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, existing_action = await pipe.mget(session_key, action_key)
                    state = self._deserialize(raw_state)
                    pipe.multi()
                    if state is not None:
                        self._assert_token(state, session_token)
                        if state.pending_action_id == action_id:
                            updated = SessionState(
                                session_id=state.session_id,
                                session_token_hash=state.session_token_hash,
                                status=state.status,
                                payer=state.payer,
                                recipient=state.recipient,
                                asset_identifier=state.asset_identifier,
                                network=state.network,
                                unit_amount=state.unit_amount,
                                min_prepay_amount=state.min_prepay_amount,
                                idle_timeout_seconds=state.idle_timeout_seconds,
                                prepaid_total=state.prepaid_total,
                                spent_total=state.spent_total,
                                available_balance=state.available_balance,
                                last_activity_at=state.last_activity_at,
                            )
                            pipe.set(session_key, self._serialize(updated), ex=self._session_ttl_seconds)
                    if existing_action is not None:
                        pipe.delete(action_key)
                    await pipe.execute()
                    return
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def close_session(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        action_id: str,
    ) -> SessionState:
        return await self._mutate(
            session_id=session_id,
            session_token=session_token,
            recipient=recipient,
            asset_identifier=asset_identifier,
            network=network,
            unit_amount=unit_amount,
            min_prepay_amount=min_prepay_amount,
            action_id=action_id,
            mutate=self._close_state,
        )

    async def get(self, session_id: str) -> SessionState | None:
        raw_state = (await self._redis.mget(self._session_key(session_id)))[0]
        return self._deserialize(raw_state)

    async def _mutate(
        self,
        *,
        session_id: str,
        session_token: str,
        recipient: str,
        asset_identifier: str,
        network: str,
        unit_amount: str,
        min_prepay_amount: str,
        action_id: str,
        mutate,
    ) -> SessionState:
        session_key = self._session_key(session_id)
        action_key = self._action_key(session_id, action_id)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(session_key, action_key)
                    raw_state, existing_action = await pipe.mget(session_key, action_key)
                    if raw_state is None:
                        raise ValueError("Session not found")
                    if existing_action is not None:
                        raise ValueError("Session action already processed")
                    state = self._deserialize(raw_state)
                    if state is None:
                        raise ValueError("Session state invalid")
                    self._assert_token(state, session_token)
                    self._assert_binding_matches(
                        state,
                        recipient=recipient,
                        asset_identifier=asset_identifier,
                        network=network,
                        unit_amount=unit_amount,
                        min_prepay_amount=min_prepay_amount,
                    )
                    updated = mutate(state)
                    pipe.multi()
                    pipe.set(session_key, self._serialize(updated), ex=self._session_ttl_seconds)
                    pipe.set(action_key, "1", ex=self._session_ttl_seconds)
                    await pipe.execute()
                    return updated
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    def _consume_state(self, state: SessionState, amount: Decimal) -> SessionState:
        self._assert_open(state)
        if amount <= Decimal("0"):
            raise ValueError("Session amount must be greater than zero")
        available = state.available_decimal()
        if available < amount:
            raise ValueError("Insufficient session balance")
        return SessionState(
            session_id=state.session_id,
            session_token_hash=state.session_token_hash,
            status=state.status,
            payer=state.payer,
            recipient=state.recipient,
            asset_identifier=state.asset_identifier,
            network=state.network,
            unit_amount=state.unit_amount,
            min_prepay_amount=state.min_prepay_amount,
            idle_timeout_seconds=state.idle_timeout_seconds,
            prepaid_total=state.prepaid_total,
            spent_total=str(state.spent_decimal() + amount),
            available_balance=str(available - amount),
            last_activity_at=self._now_iso(),
        )

    def _close_state(self, state: SessionState) -> SessionState:
        self._assert_open(state)
        return SessionState(
            session_id=state.session_id,
            session_token_hash=state.session_token_hash,
            status="closed",
            payer=state.payer,
            recipient=state.recipient,
            asset_identifier=state.asset_identifier,
            network=state.network,
            unit_amount=state.unit_amount,
            min_prepay_amount=state.min_prepay_amount,
            idle_timeout_seconds=state.idle_timeout_seconds,
            prepaid_total=state.prepaid_total,
            spent_total=state.spent_total,
            available_balance=state.available_balance,
            last_activity_at=self._now_iso(),
        )


def build_session_store(
    settings: Settings,
    redis_client: Any | None = None,
) -> SessionStore:
    if redis_client is None:
        redis_client = create_async_redis_client(settings.REDIS_URL.get_secret_value())
    return RedisSessionStore(
        redis_client,
        session_ttl_seconds=settings.SESSION_STATE_TTL_SECONDS,
        idle_timeout_seconds=settings.SESSION_IDLE_TIMEOUT_SECONDS,
    )
