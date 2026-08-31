from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from xrpl.core import binarycodec
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    DEFAULT_MAX_FEE_DROPS,
    PaymentPolicyError,
    PayChannelSessionState,
    XRPLIOUPathfindingPolicy,
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)
from xrpl_mpp_core import (
    ACCEPT_PAYMENT_HEADER,
    NETWORK_RLUSD_ISSUERS,
    NETWORK_USDC_ISSUERS,
    AcceptPaymentRange,
    IssuedCurrency,
    PaymentChallenge,
    PaymentReceipt,
    XRPLNetwork,
    decode_challenge_request,
    getenv_clean,
    normalize_currency_code,
    parse_currency,
    render_accept_payment,
    render_payment_challenge,
    serialize_currency,
)
from xrpl_mpp_core.testnet_rpc import resolve_testnet_rpc_url

from xrpl_mpp_payer.receipts import ReceiptRecord, ReceiptStore

DEFAULT_MAINNET_RPC_URL = "https://s1.ripple.com:51234"
DEFAULT_DEVNET_RPC_URL = "https://s.devnet.rippletest.net:51234"
DEFAULT_RPC_URL = DEFAULT_MAINNET_RPC_URL
DEFAULT_NETWORK: XRPLNetwork = "testnet"
DEFAULT_MAX_SPEND_ENV = "XRPL_MPP_MAX_SPEND"
EXPECTED_RECIPIENT_ENV = "XRPL_MPP_EXPECTED_RECIPIENT"
ALLOW_INSECURE_XRPL_RPC_ENV = "ALLOW_INSECURE_XRPL_RPC"
MAX_FEE_DROPS_ENV = "XRPL_MPP_MAX_FEE_DROPS"
IOU_SOURCE_CURRENCY_ENV = "XRPL_MPP_IOU_SOURCE_CURRENCY"
IOU_MAX_SOURCE_AMOUNT_ENV = "XRPL_MPP_IOU_MAX_SOURCE_AMOUNT"
IOU_SLIPPAGE_BPS_ENV = "XRPL_MPP_IOU_SLIPPAGE_BPS"
DEFAULT_TIMEOUT = 20.0


@dataclass(slots=True)
class PayResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    challenge_present: bool
    dry_run: bool
    paid: bool
    preview: dict[str, Any] | None = None
    receipt: ReceiptRecord | None = None
    payment_response: PaymentReceipt | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _NonClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


class _ReplayChallengeTransport(httpx.AsyncBaseTransport):
    """Replay the inspected 402 once, then use the real transport."""

    def __init__(
        self,
        *,
        response: httpx.Response,
        selected: PaymentChallenge,
        transport: httpx.AsyncBaseTransport,
        close_transport: bool,
    ) -> None:
        self._status_code = response.status_code
        self._content = response.content
        self._headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() != "www-authenticate"
        ]
        self._headers.append(("WWW-Authenticate", render_payment_challenge(selected)))
        self._transport = transport
        self._close_transport = close_transport
        self._replayed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._replayed:
            self._replayed = True
            return httpx.Response(
                self._status_code,
                headers=self._headers,
                content=self._content,
                request=request,
            )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        if self._close_transport:
            await self._transport.aclose()


class XRPLPayer:
    """Policy-enforcing MPP 0.2 payer for charges and XRPL PayChannels."""

    def __init__(
        self,
        signer: XRPLPaymentSigner | None,
        *,
        network: XRPLNetwork | None = None,
        store: ReceiptStore | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        expected_recipient: str | None = None,
        max_challenge_validity_seconds: int = 300,
    ) -> None:
        self.signer = signer
        self.network = _validate_network(
            network or (signer.network if signer is not None else None) or DEFAULT_NETWORK
        )
        self.store = store or ReceiptStore()
        self.timeout = timeout
        self.expected_recipient = (
            expected_recipient or getenv_clean(EXPECTED_RECIPIENT_ENV)
        )
        if max_challenge_validity_seconds <= 0:
            raise ValueError("max_challenge_validity_seconds must be positive")
        self.max_challenge_validity_seconds = max_challenge_validity_seconds
        self._channels: dict[str, PayChannelSessionState] = {}

    def register_channel(
        self,
        url: str,
        *,
        channel_id: str,
        cumulative_amount: str = "0",
        method: str = "GET",
        recipient: str,
    ) -> None:
        """Register an existing channel with its operator-approved recipient."""

        normalized_channel = _validate_channel_id(channel_id)
        normalized_cumulative = _validate_drops(cumulative_amount, name="cumulative_amount")
        self._channels[_request_key(url, method=method)] = PayChannelSessionState(
            channel_id=normalized_channel,
            cumulative_amount=normalized_cumulative,
            request_method=method.upper(),
            recipient=recipient,
            network=self.network,
        )

    def channel_state(self, url: str, *, method: str = "GET") -> PayChannelSessionState | None:
        return self._channels.get(_request_key(url, method=method))

    async def pay(
        self,
        *,
        url: str,
        amount: float | Decimal = Decimal("0.001"),
        asset: str = "XRP",
        issuer: str | None = None,
        max_spend: float | Decimal | None = None,
        dry_run: bool = False,
        intent: Literal["charge", "session"] | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        channel_id: str | None = None,
        cumulative_amount: str = "0",
        open_transaction: str | None = None,
        channel_funding_amount: str | None = None,
        channel_settle_delay: int = 3_600,
        expected_recipient: str | None = None,
    ) -> PayResult:
        """Fetch and authorize one selected MPP challenge.

        Voucher calls require a registered ``channel_id``. Opening calls
        require either a signed ``open_transaction`` or a funding amount from
        which the signer can create one. The payer makes at most one unpaid
        request and one authorized retry.
        """

        _require_secure_url(url)
        currency = resolve_currency(asset=asset, issuer=issuer, network=self.network)
        spend_cap = resolve_spend_cap(amount=amount, max_spend=max_spend)
        request_headers = _request_headers(headers, intent=intent)
        initial_response, base_transport, owns_transport = await self._probe(
            url=url,
            method=method,
            headers=request_headers,
            content=content,
            transport=transport,
        )
        payment_transport: XRPLPaymentTransport | None = None
        replay_transport: _ReplayChallengeTransport | None = None
        try:
            challenges = decode_payment_challenges_response(initial_response.headers)
            selected = _select_optional_challenge(
                challenges,
                intent=intent,
                network=self.network,
                currency=currency,
            )
            if dry_run:
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=bool(challenges),
                    dry_run=True,
                    paid=False,
                    preview=build_dry_run_preview(
                        response=initial_response,
                        selected=selected,
                        network=self.network,
                        currency=currency,
                        spend_cap=spend_cap,
                    ),
                )
            if initial_response.status_code == 402 and not challenges:
                raise ValueError("402 response did not include a valid MPP challenge")
            if selected is None:
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=False,
                    dry_run=False,
                    paid=False,
                )

            _enforce_spend_cap(selected, spend_cap)
            if self.signer is None:
                raise RuntimeError("XRPL_WALLET_SEED is required to pay MPP resources")
            approved_recipient = self._require_expected_recipient(expected_recipient)
            payment_policy = self._build_payment_policy(
                expected_recipient=approved_recipient,
                currency=currency,
                spend_cap=spend_cap,
            )
            payment_policy.authorize(selected)

            replay_transport = _ReplayChallengeTransport(
                response=initial_response,
                selected=selected,
                transport=base_transport,
                close_transport=owns_transport,
            )
            payment_transport = XRPLPaymentTransport(
                self.signer,
                network=self.network,
                currency=currency,
                base_transport=replay_transport,
                payment_preferences=[AcceptPaymentRange(method="xrpl", intent=selected.intent)],
                payment_policy=payment_policy,
            )
            await self._configure_session_transport(
                payment_transport,
                selected=selected,
                url=url,
                method=method,
                channel_id=channel_id,
                cumulative_amount=cumulative_amount,
                open_transaction=open_transaction,
                channel_funding_amount=channel_funding_amount,
                channel_settle_delay=channel_settle_delay,
                spend_cap=spend_cap,
            )
            async with httpx.AsyncClient(transport=payment_transport, timeout=self.timeout) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    content=content,
                )
                await response.aread()
                verified_session_state = (
                    payment_transport.channel_state(url, method=method)
                    if selected.intent == "session"
                    else None
                )
            payment_transport = None
            return self._result_from_response(
                response=response,
                selected=selected,
                url=url,
                method=method,
                verified_session_state=verified_session_state,
            )
        finally:
            if payment_transport is not None:
                await payment_transport.aclose()
            elif owns_transport and replay_transport is None:
                await base_transport.aclose()

    async def close_channel(
        self,
        *,
        url: str,
        channel_id: str | None = None,
        cumulative_amount: str | None = None,
        max_spend: float | Decimal | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        expected_recipient: str | None = None,
    ) -> PayResult:
        """Answer a session challenge with a final cumulative close voucher."""

        _require_secure_url(url)
        key = _request_key(url, method=method)
        current = self._channels.get(key)
        resolved_channel = channel_id or (current.channel_id if current is not None else None)
        if resolved_channel is None:
            raise ValueError("channel_id is required to close an MPP PayChannel")
        resolved_channel = _validate_channel_id(resolved_channel)
        resolved_cumulative = _validate_drops(
            cumulative_amount
            if cumulative_amount is not None
            else (current.cumulative_amount if current is not None else "0"),
            name="cumulative_amount",
        )
        request_headers = _request_headers(headers, intent="session")
        initial_response, base_transport, owns_transport = await self._probe(
            url=url,
            method=method,
            headers=request_headers,
            content=None,
            transport=transport,
        )
        payment_transport: XRPLPaymentTransport | None = None
        replay_transport: _ReplayChallengeTransport | None = None
        try:
            challenges = decode_payment_challenges_response(initial_response.headers)
            if initial_response.status_code == 402 and not challenges:
                raise ValueError("402 response did not include a valid MPP challenge")
            selected = _select_optional_challenge(
                challenges,
                intent="session",
                network=self.network,
                currency="XRP",
            )
            if selected is None:
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=False,
                    dry_run=False,
                    paid=False,
                )
            terms = decode_challenge_request(selected)
            if not terms.channel_id or terms.channel_id.upper() != resolved_channel:
                raise ValueError("server challenge channelId does not match the registered channel")
            if current is not None and current.recipient is not None:
                if current.recipient != terms.recipient:
                    raise ValueError(
                        "server challenge recipient does not match the registered channel"
                    )
            challenge_cumulative = (
                terms.method_details.cumulative_amount
                if terms.method_details is not None
                and terms.method_details.cumulative_amount is not None
                else "0"
            )
            if challenge_cumulative != resolved_cumulative:
                raise ValueError(
                    "server challenge cumulativeAmount does not match the registered channel"
                )
            spend_cap = resolve_spend_cap(
                amount=Decimal("0.001"),
                max_spend=max_spend,
            )
            _enforce_spend_cap(selected, spend_cap)
            if self.signer is None:
                raise RuntimeError("XRPL_WALLET_SEED is required to close MPP PayChannels")
            approved_recipient = (
                expected_recipient
                or self.expected_recipient
                or (current.recipient if current is not None else None)
            )
            if approved_recipient is None:
                raise PaymentPolicyError(
                    "expected_recipient is required to close an MPP PayChannel"
                )
            payment_policy = self._build_payment_policy(
                expected_recipient=approved_recipient,
                currency="XRP",
                spend_cap=spend_cap,
            )
            payment_policy.authorize(selected)

            replay_transport = _ReplayChallengeTransport(
                response=initial_response,
                selected=selected,
                transport=base_transport,
                close_transport=owns_transport,
            )
            payment_transport = XRPLPaymentTransport(
                self.signer,
                network=self.network,
                currency="XRP",
                base_transport=replay_transport,
                payment_preferences=[AcceptPaymentRange(method="xrpl", intent="session")],
                payment_policy=payment_policy,
            )
            payment_transport.register_channel(
                url,
                channel_id=resolved_channel,
                cumulative_amount=resolved_cumulative,
                method=method,
                recipient=terms.recipient,
                network=self.network,
            )
            response = await payment_transport.close_session(
                url,
                method=method,
                headers=request_headers,
            )
            await response.aread()
            verified_session_closed = (
                payment_transport.channel_state(url, method=method) is None
            )
            await payment_transport.aclose()
            payment_transport = None
            result = self._result_from_response(
                response=response,
                selected=selected,
                url=url,
                method=method,
                verified_session_closed=verified_session_closed,
            )
            return result
        finally:
            if payment_transport is not None:
                await payment_transport.aclose()
            elif owns_transport and replay_transport is None:
                await base_transport.aclose()

    async def _probe(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        content: bytes | None,
        transport: httpx.AsyncBaseTransport | None,
    ) -> tuple[httpx.Response, httpx.AsyncBaseTransport, bool]:
        base_transport = transport or httpx.AsyncHTTPTransport()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=_NonClosingTransport(base_transport),
            ) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=content,
                )
                await response.aread()
        except Exception:
            if transport is None:
                await base_transport.aclose()
            raise
        return response, base_transport, transport is None

    async def _configure_session_transport(
        self,
        payment_transport: XRPLPaymentTransport,
        *,
        selected: PaymentChallenge,
        url: str,
        method: str,
        channel_id: str | None,
        cumulative_amount: str,
        open_transaction: str | None,
        channel_funding_amount: str | None,
        channel_settle_delay: int,
        spend_cap: Decimal | None,
    ) -> None:
        session_arguments_present = any(
            value is not None
            for value in (channel_id, open_transaction, channel_funding_amount)
        )
        if selected.intent != "session":
            if session_arguments_present:
                raise ValueError("PayChannel options require an xrpl/session challenge")
            return

        terms = decode_challenge_request(selected)
        key = _request_key(url, method=method)
        if not terms.channel_id:
            if channel_id is not None:
                raise ValueError("channel_id cannot be used with a PayChannel open challenge")
            transaction = open_transaction
            if transaction is None and channel_funding_amount is not None:
                funding_amount = _validate_drops(
                    channel_funding_amount,
                    name="channel_funding_amount",
                )
                _enforce_funding_cap(funding_amount, spend_cap)
                transaction = await self.signer.sign_channel_create_async(
                    destination=terms.recipient,
                    funding_amount=funding_amount,
                    settle_delay=channel_settle_delay,
                    challenge_expires=selected.expires,
                )
            if transaction is None:
                raise ValueError(
                    "PayChannel open requires open_transaction or channel_funding_amount"
                )
            _validate_open_transaction(
                transaction,
                signer=self.signer,
                recipient=terms.recipient,
                spend_cap=spend_cap,
            )
            payment_transport.register_open_transaction(
                url,
                transaction=transaction,
                method=method,
            )
            return

        state = self._channels.get(key)
        if channel_id is not None:
            normalized_channel = _validate_channel_id(channel_id)
            if normalized_channel != terms.channel_id.upper():
                raise ValueError(
                    "server challenge channelId does not match the requested channel"
                )
            self.register_channel(
                url,
                channel_id=normalized_channel,
                cumulative_amount=cumulative_amount,
                method=method,
                recipient=terms.recipient,
            )
            state = self._channels[key]
        if state is None:
            raise ValueError(
                "PayChannel voucher requires channel_id or a previously registered channel"
            )
        if state.channel_id.upper() != terms.channel_id.upper():
            raise ValueError("server challenge channelId does not match the registered channel")
        if state.recipient is not None and state.recipient != terms.recipient:
            raise ValueError("server challenge recipient does not match the registered channel")
        challenge_cumulative = (
            terms.method_details.cumulative_amount
            if terms.method_details is not None
            and terms.method_details.cumulative_amount is not None
            else "0"
        )
        if challenge_cumulative != state.cumulative_amount:
            raise ValueError(
                "server challenge cumulativeAmount does not match the registered channel"
            )
        payment_transport.register_channel(
            url,
            channel_id=state.channel_id,
            cumulative_amount=state.cumulative_amount,
            method=method,
            recipient=state.recipient or terms.recipient,
            network=self.network,
        )

    def _require_expected_recipient(self, value: str | None) -> str:
        if (
            value is not None
            and self.expected_recipient is not None
            and value != self.expected_recipient
        ):
            raise PaymentPolicyError(
                "expected_recipient cannot override the configured operator recipient"
            )
        recipient = self.expected_recipient or value
        if recipient is None:
            raise PaymentPolicyError(
                "expected_recipient is required for automatic MPP payment"
            )
        return recipient

    def _build_payment_policy(
        self,
        *,
        expected_recipient: str,
        currency: str,
        spend_cap: Decimal | None,
    ) -> XRPLPaymentPolicy:
        if spend_cap is None:
            raise PaymentPolicyError(
                "a finite spend cap is required for automatic MPP payment"
            )
        max_amount = (
            spend_cap * Decimal("1000000")
            if parse_currency(currency) == "XRP"
            else spend_cap
        )
        return XRPLPaymentPolicy(
            expected_recipients=expected_recipient,
            max_amount=str(max_amount),
            allowed_currencies=[currency],
            max_challenge_validity_seconds=self.max_challenge_validity_seconds,
        )

    def _result_from_response(
        self,
        *,
        response: httpx.Response,
        selected: PaymentChallenge,
        url: str,
        method: str,
        verified_session_state: PayChannelSessionState | None = None,
        verified_session_closed: bool = False,
    ) -> PayResult:
        payment_receipt = decode_payment_receipt_header(response.headers)
        receipt = None
        if payment_receipt is not None and 200 <= response.status_code < 300:
            _validate_receipt_binding(payment_receipt, selected)
            receipt = build_receipt_record(
                url=url,
                method=method,
                status_code=response.status_code,
                payment_receipt=payment_receipt,
                payment_challenge=selected,
                default_network=self.network,
            )
            self.store.append(receipt)
            self._capture_channel_state(
                url=url,
                method=method,
                payment_receipt=payment_receipt,
                payment_challenge=selected,
                verified_session_state=verified_session_state,
                verified_session_closed=verified_session_closed,
            )
        return PayResult(
            status_code=response.status_code,
            body=response.content,
            headers=dict(response.headers),
            challenge_present=True,
            dry_run=False,
            paid=receipt is not None,
            receipt=receipt,
            payment_response=payment_receipt,
        )

    def _capture_channel_state(
        self,
        *,
        url: str,
        method: str,
        payment_receipt: PaymentReceipt,
        payment_challenge: PaymentChallenge,
        verified_session_state: PayChannelSessionState | None,
        verified_session_closed: bool,
    ) -> None:
        del payment_receipt
        if payment_challenge.intent != "session":
            return
        key = _request_key(url, method=method)
        if verified_session_closed:
            self._channels.pop(key, None)
            return
        if verified_session_state is None:
            return
        self._channels[key] = verified_session_state


async def pay_with_mpp(**kwargs: Any) -> PayResult:
    signer = kwargs.pop("signer", None)
    rpc_url = kwargs.pop("rpc_url", None)
    timeout = float(kwargs.pop("timeout", DEFAULT_TIMEOUT))
    requested_network = kwargs.pop("network", None)
    dry_run = bool(kwargs.get("dry_run", False))
    if signer is None and not dry_run:
        signer = build_signer_from_env(rpc_url=rpc_url, network=requested_network)
    network = _validate_network(
        requested_network
        or (signer.network if signer is not None else None)
        or DEFAULT_NETWORK
    )
    store = kwargs.pop("store", None)
    payer = XRPLPayer(signer, network=network, store=store, timeout=timeout)
    return await payer.pay(**kwargs)


async def close_with_mpp(**kwargs: Any) -> PayResult:
    """Build an environment-backed payer and close one PayChannel."""

    signer = kwargs.pop("signer", None)
    rpc_url = kwargs.pop("rpc_url", None)
    timeout = float(kwargs.pop("timeout", DEFAULT_TIMEOUT))
    requested_network = kwargs.pop("network", None)
    if signer is None:
        signer = build_signer_from_env(rpc_url=rpc_url, network=requested_network)
    network = _validate_network(requested_network or signer.network or DEFAULT_NETWORK)
    store = kwargs.pop("store", None)
    payer = XRPLPayer(signer, network=network, store=store, timeout=timeout)
    return await payer.close_channel(**kwargs)


def resolve_signer_rpc_url(
    *,
    rpc_url: str | None = None,
    network: XRPLNetwork,
) -> str:
    resolved_rpc_url = (rpc_url or "").strip() or getenv_clean("XRPL_RPC_URL")
    if resolved_rpc_url:
        return resolved_rpc_url
    if network == "testnet":
        return resolve_testnet_rpc_url()
    if network == "devnet":
        return DEFAULT_DEVNET_RPC_URL
    return DEFAULT_MAINNET_RPC_URL


def build_signer_from_env(
    *,
    rpc_url: str | None = None,
    network: str | None = None,
    allow_insecure_rpc: bool | None = None,
    max_fee_drops: str | None = None,
    iou_pathfinding_policy: XRPLIOUPathfindingPolicy | None = None,
) -> XRPLPaymentSigner:
    wallet_seed = getenv_clean("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to pay MPP resources")
    wallet = Wallet.from_seed(wallet_seed)
    resolved_network = _validate_network(
        network
        or getenv_clean("XRPL_NETWORK")
        or getenv_clean("NETWORK_ID")
        or DEFAULT_NETWORK
    )
    resolved_rpc_url = resolve_signer_rpc_url(
        rpc_url=rpc_url,
        network=resolved_network,
    )
    resolved_allow_insecure_rpc = _resolve_boolean_env(
        ALLOW_INSECURE_XRPL_RPC_ENV,
        explicit=allow_insecure_rpc,
    )
    resolved_max_fee_drops = (
        max_fee_drops
        if max_fee_drops is not None
        else getenv_clean(MAX_FEE_DROPS_ENV) or DEFAULT_MAX_FEE_DROPS
    )
    resolved_iou_pathfinding_policy = (
        iou_pathfinding_policy
        if iou_pathfinding_policy is not None
        else _iou_pathfinding_policy_from_env()
    )
    return XRPLPaymentSigner(
        wallet,
        rpc_url=resolved_rpc_url,
        network=resolved_network,
        allow_insecure_rpc=resolved_allow_insecure_rpc,
        max_fee_drops=resolved_max_fee_drops,
        iou_pathfinding_policy=resolved_iou_pathfinding_policy,
    )


def _resolve_boolean_env(name: str, *, explicit: bool | None = None) -> bool:
    if explicit is not None:
        if not isinstance(explicit, bool):
            raise ValueError(f"{name} must be a boolean")
        return explicit
    raw = getenv_clean(name)
    if raw is None:
        return False
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off"
    )


def _iou_pathfinding_policy_from_env() -> XRPLIOUPathfindingPolicy | None:
    source_currency = getenv_clean(IOU_SOURCE_CURRENCY_ENV)
    max_source_amount = getenv_clean(IOU_MAX_SOURCE_AMOUNT_ENV)
    if source_currency is None and max_source_amount is None:
        return None
    if source_currency is None or max_source_amount is None:
        raise ValueError(
            f"{IOU_SOURCE_CURRENCY_ENV} and {IOU_MAX_SOURCE_AMOUNT_ENV} "
            "must be configured together"
        )
    raw_slippage = getenv_clean(IOU_SLIPPAGE_BPS_ENV) or "50"
    if not raw_slippage.isascii() or not raw_slippage.isdigit():
        raise ValueError(f"{IOU_SLIPPAGE_BPS_ENV} must be an integer from 0 to 1000")
    return XRPLIOUPathfindingPolicy(
        source_currency=source_currency,
        max_source_amount=max_source_amount,
        slippage_bps=int(raw_slippage),
    )


def resolve_currency(*, asset: str, issuer: str | None, network: XRPLNetwork) -> str:
    """Resolve CLI-friendly input to the canonical MPP 0.2 currency string."""

    raw_asset = asset.strip()
    if raw_asset.startswith("{"):
        if issuer is not None:
            raise ValueError("issuer cannot be combined with a JSON currency descriptor")
        return serialize_currency(parse_currency(raw_asset))
    normalized_asset = normalize_currency_code(raw_asset)
    if normalized_asset == "XRP":
        if issuer is not None:
            raise ValueError("XRP does not use an issuer")
        return "XRP"
    normalized_issuer = issuer.strip() if issuer else None
    if normalized_issuer is None and normalized_asset == "RLUSD":
        normalized_issuer = NETWORK_RLUSD_ISSUERS.get(network)
    if normalized_issuer is None and normalized_asset == "USDC":
        normalized_issuer = NETWORK_USDC_ISSUERS.get(network)
    if normalized_issuer is None:
        raise ValueError(f"Issuer is required for asset {normalized_asset}")
    return serialize_currency(
        IssuedCurrency(currency=normalized_asset, issuer=normalized_issuer)
    )


def resolve_spend_cap(
    *,
    amount: float | Decimal,
    max_spend: float | Decimal | None,
) -> Decimal:
    requested = Decimal(str(max_spend if max_spend is not None else amount))
    env_value = getenv_clean(DEFAULT_MAX_SPEND_ENV)
    operator_ceiling = Decimal(env_value) if env_value is not None else None
    values = (requested,) if operator_ceiling is None else (requested, operator_ceiling)
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("spend cap must be a non-negative finite amount")
    return min(values)


def payment_request_amount(request: Any) -> Decimal:
    if parse_currency(request.currency) == "XRP":
        return Decimal(request.amount) / Decimal("1000000")
    return Decimal(request.amount)


def payment_challenge_amount(challenge: PaymentChallenge) -> Decimal:
    """Return the incremental amount requested by charge or session terms."""

    return payment_request_amount(decode_challenge_request(challenge))


def build_receipt_record(
    *,
    url: str,
    method: str,
    status_code: int,
    payment_receipt: PaymentReceipt,
    payment_challenge: PaymentChallenge,
    default_network: XRPLNetwork,
) -> ReceiptRecord:
    request = decode_challenge_request(payment_challenge)
    details = request.method_details
    receipt_network = payment_receipt.network or (
        details.network if details is not None else None
    ) or default_network
    return ReceiptRecord(
        created_at=datetime.now(UTC).isoformat(),
        url=url,
        method=method.upper(),
        status_code=status_code,
        network=_validate_network(receipt_network),
        currency=request.currency,
        amount=str(payment_request_amount(request)),
        payer=payment_receipt.payer or "",
        reference=payment_receipt.reference,
        settlement_status=payment_receipt.settlement_status or "success",
        intent=payment_challenge.intent,
        action=payment_receipt.action,
        channel_id=payment_receipt.channel_id,
        cumulative=payment_receipt.cumulative,
        transaction_hash=payment_receipt.tx_hash,
    )


def build_dry_run_preview(
    *,
    response: httpx.Response,
    selected: PaymentChallenge | None,
    network: XRPLNetwork,
    currency: str,
    spend_cap: Decimal | None,
) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "mode": "dry_run",
        "status_code": response.status_code,
        "url": str(response.request.url),
        "network": network,
        "currency": currency,
        "spend_cap": str(spend_cap) if spend_cap is not None else None,
        "mpp_challenge_present": selected is not None,
    }
    if selected is None:
        preview["message"] = "No matching MPP challenge detected; no payment attempted."
        return preview
    request = decode_challenge_request(selected)
    requested_amount = payment_challenge_amount(selected)
    preview["selected_payment"] = {
        "intent": selected.intent,
        "recipient": request.recipient,
        "amount": str(requested_amount),
        "currency": request.currency,
        "channel_id": getattr(request, "channel_id", None),
    }
    preview["would_pay"] = spend_cap is None or requested_amount <= spend_cap
    return preview


def format_pay_result(result: PayResult) -> str:
    if result.preview is not None:
        return json.dumps(result.preview, indent=2, sort_keys=True)
    if result.text.strip():
        return result.text
    summary = {
        "status_code": result.status_code,
        "paid": result.paid,
        "receipt": result.receipt.model_dump() if result.receipt is not None else None,
    }
    return json.dumps(summary, indent=2, sort_keys=True)


def get_receipts(limit: int = 10, *, store: ReceiptStore | None = None) -> list[dict[str, Any]]:
    active_store = store or ReceiptStore()
    return [receipt.model_dump() for receipt in active_store.list(limit=limit)]


def budget_status(
    *,
    asset: str = "XRP",
    issuer: str | None = None,
    network: str | None = None,
    store: ReceiptStore | None = None,
) -> dict[str, str | None]:
    resolved_network = _validate_network(
        network
        or getenv_clean("XRPL_NETWORK")
        or getenv_clean("NETWORK_ID")
        or DEFAULT_NETWORK
    )
    currency = resolve_currency(asset=asset, issuer=issuer, network=resolved_network)
    env_cap = getenv_clean(DEFAULT_MAX_SPEND_ENV)
    max_spend = Decimal(env_cap) if env_cap else None
    active_store = store or ReceiptStore()
    return active_store.budget_summary(currency=currency, max_spend=max_spend)


def _validate_network(value: str) -> XRPLNetwork:
    if value not in {"mainnet", "testnet", "devnet"}:
        raise ValueError("network must be mainnet, testnet, or devnet")
    return value  # type: ignore[return-value]


def _validate_channel_id(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 64 or any(char not in "0123456789ABCDEF" for char in normalized):
        raise ValueError("channel_id must be 64 hexadecimal characters")
    return normalized


def _validate_drops(value: str, *, name: str) -> str:
    normalized = str(value)
    if not normalized.isascii() or not normalized.isdigit():
        raise ValueError(f"{name} must be an unsigned drops string")
    return normalized


def _request_key(url: str, *, method: str) -> str:
    parts = urlsplit(url)
    resource = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if parts.query:
        resource = f"{resource}?{parts.query}"
    return f"{method.upper()} {resource}"


def _request_headers(
    headers: dict[str, str] | None,
    *,
    intent: Literal["charge", "session"] | None,
) -> dict[str, str]:
    result = dict(headers or {})
    if not any(name.lower() == ACCEPT_PAYMENT_HEADER.lower() for name in result):
        preferences = (
            [AcceptPaymentRange(method="xrpl", intent=intent)]
            if intent is not None
            else [
                AcceptPaymentRange(method="xrpl", intent="charge"),
                AcceptPaymentRange(method="xrpl", intent="session"),
            ]
        )
        result[ACCEPT_PAYMENT_HEADER] = render_accept_payment(preferences)
    return result


def _select_optional_challenge(
    challenges: list[PaymentChallenge],
    *,
    intent: Literal["charge", "session"] | None,
    network: XRPLNetwork,
    currency: str,
) -> PaymentChallenge | None:
    if not challenges:
        return None
    return select_payment_challenge(
        challenges,
        intent=intent,
        network=network,
        currency=currency,
    )


def _enforce_spend_cap(challenge: PaymentChallenge, spend_cap: Decimal | None) -> None:
    requested_amount = payment_challenge_amount(challenge)
    if spend_cap is not None and requested_amount > spend_cap:
        raise ValueError(
            f"Payment amount {requested_amount} exceeds configured spend cap {spend_cap}"
        )


def _enforce_funding_cap(funding_drops: str, spend_cap: Decimal | None) -> None:
    funding_xrp = Decimal(funding_drops) / Decimal("1000000")
    if spend_cap is not None and funding_xrp > spend_cap:
        raise ValueError(
            f"PayChannel funding amount {funding_xrp} exceeds configured spend cap {spend_cap}"
        )


def _validate_open_transaction(
    transaction: str,
    *,
    signer: XRPLPaymentSigner,
    recipient: str,
    spend_cap: Decimal | None,
) -> None:
    try:
        decoded = binarycodec.decode(transaction)
    except Exception as exc:
        raise ValueError("open_transaction must be a valid signed XRPL transaction") from exc
    if decoded.get("TransactionType") != "PaymentChannelCreate":
        raise ValueError("open_transaction must be a PaymentChannelCreate")
    if decoded.get("Account") != signer.wallet.classic_address:
        raise ValueError("open_transaction account does not match the payer signer")
    if decoded.get("Destination") != recipient:
        raise ValueError("open_transaction destination does not match the challenge recipient")
    if decoded.get("PublicKey") != signer.wallet.public_key:
        raise ValueError("open_transaction public key does not match the payer signer")
    funding_drops = decoded.get("Amount")
    if not isinstance(funding_drops, str):
        raise ValueError("open_transaction Amount must be XRP drops")
    _enforce_funding_cap(
        _validate_drops(funding_drops, name="open_transaction Amount"),
        spend_cap,
    )


def _validate_receipt_binding(
    receipt: PaymentReceipt,
    challenge: PaymentChallenge,
) -> None:
    if receipt.method != challenge.method:
        raise ValueError("Payment-Receipt method does not match the selected challenge")
    if receipt.challenge_id is not None and receipt.challenge_id != challenge.id:
        raise ValueError("Payment-Receipt challengeId does not match the selected challenge")
    receipt_intent = (receipt.model_extra or {}).get("intent")
    if receipt_intent is not None and receipt_intent != challenge.intent:
        raise ValueError("Payment-Receipt intent does not match the selected challenge")
    receipt_external_id = (receipt.model_extra or {}).get("externalId")
    if receipt_external_id is not None and receipt_external_id != challenge.id:
        raise ValueError(
            "Payment-Receipt externalId does not match the selected challenge ID"
        )
    terms = decode_challenge_request(challenge)
    if receipt.recipient is not None and receipt.recipient != terms.recipient:
        raise ValueError("Payment-Receipt recipient does not match the selected challenge")
    details = terms.method_details
    expected_network = details.network if details is not None else None
    if (
        receipt.network is not None
        and expected_network is not None
        and receipt.network != expected_network
    ):
        raise ValueError("Payment-Receipt network does not match the selected challenge")
    if challenge.intent != "session":
        return
    if terms.channel_id and receipt.channel_id is not None:
        if receipt.channel_id.upper() != terms.channel_id.upper():
            raise ValueError("Payment-Receipt channelId does not match the selected challenge")
    if receipt.cumulative is not None:
        previous = (
            details.cumulative_amount
            if details is not None and details.cumulative_amount is not None
            else "0"
        )
        expected_cumulative = str(int(previous) + int(terms.amount))
        if receipt.cumulative != expected_cumulative:
            raise ValueError(
                "Payment-Receipt cumulative does not match the selected challenge"
            )


def _require_secure_url(url: str) -> None:
    parsed = httpx.URL(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.host or "").lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return
    raise ValueError("MPP requests require HTTPS except on loopback")
