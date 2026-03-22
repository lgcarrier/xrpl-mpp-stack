from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import hashlib
import re
import secrets
from typing import Any, Callable, Mapping, Protocol

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xrpl_mpp_core import (
    MPPProblemDetails,
    PaymentChallenge,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_content_digest,
    build_payment_challenge,
    canonical_asset_identifier,
    decode_session_payload,
    encode_payment_receipt,
    parse_payment_authorization_header,
    render_payment_challenge,
    xrpl_asset_from_identifier,
)
from xrpl_mpp_middleware.client import XRPLFacilitatorClient
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    FacilitatorTransportError,
    InvalidPaymentHeaderError,
    RouteConfigurationError,
)
from xrpl_mpp_middleware.types import ChargeRouteSpec, RouteConfig, SessionRouteSpec

WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"
AUTHORIZATION_HEADER = "Authorization"
PAYMENT_RECEIPT_HEADER = "Payment-Receipt"
SESSION_ID_HEADER = "X-MPP-Session-Id"
HEX_64_PATTERN = re.compile(r"^[0-9A-F]{64}$")
DEFAULT_MAX_REQUEST_BODY_BYTES = 32_768
REQUEST_BODY_TOO_LARGE_DETAIL = "Request body too large"


class FacilitatorClientProtocol(Protocol):
    async def startup(self) -> None:
        ...

    async def aclose(self) -> None:
        ...

    async def get_supported(self, *, force_refresh: bool = False):
        ...

    async def charge(self, credential):
        ...

    async def session(self, credential):
        ...


class RequestBodyTooLargeError(Exception):
    pass


class PaymentMiddlewareASGI:
    def __init__(
        self,
        app: ASGIApp,
        *,
        route_configs: Mapping[str, RouteConfig | dict[str, Any]],
        challenge_secret: str,
        challenge_ttl_seconds: int = 300,
        default_realm: str | None = None,
        client_factory: Callable[[str, str], FacilitatorClientProtocol] | None = None,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.app = app
        self._challenge_secret = challenge_secret.strip()
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._default_realm = default_realm.strip() if default_realm else None
        self._client_factory = client_factory or self._default_client_factory
        self._max_request_body_bytes = max_request_body_bytes
        self._session_id_factory = session_id_factory or (lambda: secrets.token_hex(32).upper())
        self._startup_lock = asyncio.Lock()
        self._started = False
        self._routes: dict[tuple[str, str], RouteConfig] = {}
        self._clients: dict[tuple[str, str], FacilitatorClientProtocol] = {}

        if not self._challenge_secret:
            raise RouteConfigurationError("challenge_secret is required")
        if challenge_ttl_seconds <= 0:
            raise RouteConfigurationError("challenge_ttl_seconds must be greater than zero")
        if max_request_body_bytes <= 0:
            raise RouteConfigurationError("max_request_body_bytes must be greater than zero")

        for route_key, route_config in route_configs.items():
            method, path = self._parse_route_key(route_key)
            self._routes[(method, path)] = (
                route_config
                if isinstance(route_config, RouteConfig)
                else RouteConfig.model_validate(route_config)
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route_config = self._routes.get((scope["method"].upper(), scope["path"]))
        if route_config is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_request_body_bytes:
                    await self._send_request_too_large(send, receive, scope)
                    return
            except ValueError:
                pass

        try:
            body = await self._read_body(receive, max_body_bytes=self._max_request_body_bytes)
        except RequestBodyTooLargeError:
            await self._send_request_too_large(send, receive, scope)
            return

        await self.startup()

        body_digest = build_content_digest(body)

        authorization = headers.get(AUTHORIZATION_HEADER)
        if authorization is None:
            await self._send_challenge(route_config, headers, body_digest, send=send, receive=receive, scope=scope)
            return

        try:
            credential = parse_payment_authorization_header(authorization)
        except ValueError as exc:
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error=str(exc),
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if credential.challenge.digest and credential.challenge.digest != body_digest:
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error="Request body digest does not match the payment challenge",
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if credential.challenge.intent == "charge":
            if not route_config.charge_options:
                await self._send_challenge(
                    route_config,
                    headers,
                    body_digest,
                    error="Charge is not accepted for this route",
                    send=send,
                    receive=receive,
                    scope=scope,
                )
                return
            await self._handle_charge(route_config, credential, body, scope, receive, send)
            return

        if credential.challenge.intent == "session":
            if not route_config.session_options:
                await self._send_challenge(
                    route_config,
                    headers,
                    body_digest,
                    error="Session is not accepted for this route",
                    send=send,
                    receive=receive,
                    scope=scope,
                )
                return
            await self._handle_session(route_config, credential, body, scope, receive, send)
            return

        await self._send_challenge(
            route_config,
            headers,
            body_digest,
            error="Unsupported payment intent",
            send=send,
            receive=receive,
            scope=scope,
        )

    async def startup(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            for route_key, route_config in self._routes.items():
                client = self._get_client(route_config)
                await client.startup()
                supported = await client.get_supported()
                self._validate_route_support(route_key, route_config, supported)
            self._started = True

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._started = False

    async def _handle_charge(
        self,
        route_config: RouteConfig,
        credential,
        body: bytes,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        client = self._get_client(route_config)
        try:
            receipt = await client.charge(credential)
        except FacilitatorPaymentError as exc:
            await self._send_challenge(
                route_config,
                Headers(scope=scope),
                build_content_digest(body),
                error=exc.detail,
                send=send,
                receive=receive,
                scope=scope,
            )
            return
        except FacilitatorTransportError as exc:
            await self._send_error(send, receive, scope, 503, str(exc))
            return
        except FacilitatorProtocolError as exc:
            await self._send_error(send, receive, scope, 502, str(exc))
            return

        await self._forward_paid_request(
            receipt=receipt,
            body=body,
            scope=scope,
            receive=receive,
            send=send,
        )

    async def _handle_session(
        self,
        route_config: RouteConfig,
        credential,
        body: bytes,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        client = self._get_client(route_config)
        try:
            receipt = await client.session(credential)
        except FacilitatorPaymentError as exc:
            await self._send_challenge(
                route_config,
                Headers(scope=scope),
                build_content_digest(body),
                error=exc.detail,
                send=send,
                receive=receive,
                scope=scope,
            )
            return
        except FacilitatorTransportError as exc:
            await self._send_error(send, receive, scope, 503, str(exc))
            return
        except FacilitatorProtocolError as exc:
            await self._send_error(send, receive, scope, 502, str(exc))
            return

        payload = decode_session_payload(credential)
        if payload.action in {"top_up", "close"}:
            response = JSONResponse(
                status_code=200,
                content=receipt.model_dump(by_alias=True, exclude_none=True),
            )
            response.headers[PAYMENT_RECEIPT_HEADER] = encode_payment_receipt(receipt)
            response.headers["Cache-Control"] = "private"
            await response(scope, receive, send)
            return

        await self._forward_paid_request(
            receipt=receipt,
            body=body,
            scope=scope,
            receive=receive,
            send=send,
        )

    async def _forward_paid_request(
        self,
        *,
        receipt: PaymentReceipt,
        body: bytes,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        scope.setdefault("state", {})
        scope["state"]["mpp_payment"] = receipt

        async def send_with_receipt(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers[PAYMENT_RECEIPT_HEADER] = encode_payment_receipt(receipt)
                headers["Cache-Control"] = "private"
            await send(message)

        try:
            await self.app(scope, self._replay_body(body), send_with_receipt)
        except Exception:
            await self._send_paid_internal_error(send, receive, scope, receipt)

    @staticmethod
    def _parse_route_key(route_key: str) -> tuple[str, str]:
        method, separator, path = route_key.partition(" ")
        if not separator or not path.startswith("/"):
            raise RouteConfigurationError(f"Route key '{route_key}' must use the format 'METHOD /path'")
        return method.upper(), path

    @staticmethod
    def _default_client_factory(
        facilitator_url: str,
        bearer_token: str,
    ) -> FacilitatorClientProtocol:
        return XRPLFacilitatorClient(base_url=facilitator_url, bearer_token=bearer_token)

    def _get_client(self, route_config: RouteConfig) -> FacilitatorClientProtocol:
        client_key = (route_config.facilitator_url, route_config.bearer_token)
        client = self._clients.get(client_key)
        if client is None:
            client = self._client_factory(*client_key)
            self._clients[client_key] = client
        return client

    def _validate_route_support(
        self,
        route_key: tuple[str, str],
        route_config: RouteConfig,
        supported,
    ) -> None:
        method_info = next((item for item in supported.methods if item.method == "xrpl"), None)
        if method_info is None:
            raise RouteConfigurationError("Facilitator does not advertise the xrpl payment method")

        supported_assets = {canonical_asset_identifier(asset) for asset in method_info.assets}
        supported_intents = set(method_info.intents)

        for option in route_config.charge_options:
            if "charge" not in supported_intents:
                raise RouteConfigurationError("Facilitator does not advertise charge support")
            self._validate_option_network_asset(route_key, option.network, option.asset_identifier, method_info.network, supported_assets)

        for option in route_config.session_options:
            if "session" not in supported_intents:
                raise RouteConfigurationError("Facilitator does not advertise session support")
            self._validate_option_network_asset(route_key, option.network, option.asset_identifier, method_info.network, supported_assets)

    @staticmethod
    def _validate_option_network_asset(
        route_key: tuple[str, str],
        network: str,
        asset_identifier: str,
        supported_network: str,
        supported_assets: set[str],
    ) -> None:
        method, path = route_key
        if network != supported_network:
            raise RouteConfigurationError(
                f"{method} {path} expects {network}, but facilitator supports {supported_network}"
            )
        if asset_identifier not in supported_assets:
            raise RouteConfigurationError(
                f"{method} {path} uses unsupported asset {asset_identifier}"
            )

    async def _send_challenge(
        self,
        route_config: RouteConfig,
        headers: Headers,
        body_digest: str | None,
        *,
        send: Send,
        receive: Receive,
        scope: Scope,
        error: str | None = None,
    ) -> None:
        challenges = self._build_challenges(route_config, headers, body_digest)
        problem = MPPProblemDetails(
            type="https://paymentauth.org/problems/payment-required" if error is None else "https://paymentauth.org/problems/verification-failed",
            title="Payment Required" if error is None else "Payment verification failed",
            status=402,
            detail=error or "Payment required for this resource",
        )
        response = JSONResponse(status_code=402, content=problem.model_dump(by_alias=True, exclude_none=True))
        response.headers["Cache-Control"] = "no-store"
        for challenge in challenges:
            response.raw_headers.append(
                (b"www-authenticate", render_payment_challenge(challenge).encode("utf-8"))
            )
        await response(scope, receive, send)

    def _build_challenges(
        self,
        route_config: RouteConfig,
        headers: Headers,
        body_digest: str | None,
    ) -> list[PaymentChallenge]:
        realm = route_config.realm or self._default_realm or headers.get("host") or "localhost"
        challenges: list[PaymentChallenge] = []

        for option in route_config.charge_options:
            request = XRPLChargeRequest(
                amount=option.amount,
                currency=option.asset_identifier,
                recipient=option.recipient,
                description=option.description or route_config.description,
                externalId=option.external_id,
                methodDetails=XRPLChargeMethodDetails(
                    network=option.network,
                    invoiceId=self._normalize_xrpl_reference_id(secrets.token_hex(32).upper()),
                ),
            )
            challenges.append(
                build_payment_challenge(
                    secret=self._challenge_secret,
                    realm=realm,
                    method="xrpl",
                    intent="charge",
                    request_model=request,
                    expires_in_seconds=self._challenge_ttl_seconds,
                    description=option.description or route_config.description,
                    digest=body_digest,
                )
            )

        for option in route_config.session_options:
            requested_session_id = self._normalize_xrpl_reference_id(
                headers.get(SESSION_ID_HEADER) or self._session_id_factory()
            )
            request = XRPLSessionRequest(
                amount=option.amount,
                currency=option.asset_identifier,
                recipient=option.recipient,
                description=option.description or route_config.description,
                externalId=option.external_id,
                methodDetails=XRPLSessionMethodDetails(
                    network=option.network,
                    sessionId=requested_session_id,
                    asset=option.asset_identifier,
                    unitAmount=option.unit_amount or option.amount,
                    minPrepayAmount=option.min_prepay_amount,
                    idleTimeoutSeconds=option.idle_timeout_seconds,
                    meteringHints=option.metering_hints,
                ),
            )
            challenges.append(
                build_payment_challenge(
                    secret=self._challenge_secret,
                    realm=realm,
                    method="xrpl",
                    intent="session",
                    request_model=request,
                    expires_in_seconds=self._challenge_ttl_seconds,
                    description=option.description or route_config.description,
                    digest=body_digest,
                )
            )
        return challenges

    @staticmethod
    def _normalize_xrpl_reference_id(value: str) -> str:
        normalized = value.strip().upper()
        if HEX_64_PATTERN.fullmatch(normalized):
            return normalized
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()

    @staticmethod
    async def _send_error(
        send: Send,
        receive: Receive,
        scope: Scope,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)

    @staticmethod
    async def _send_paid_internal_error(
        send: Send,
        receive: Receive,
        scope: Scope,
        receipt: PaymentReceipt,
    ) -> None:
        response = JSONResponse(
            status_code=500,
            content={"detail": "The protected application failed after payment settlement"},
        )
        response.headers[PAYMENT_RECEIPT_HEADER] = encode_payment_receipt(receipt)
        response.headers["Cache-Control"] = "private"
        await response(scope, receive, send)

    @staticmethod
    async def _send_request_too_large(send: Send, receive: Receive, scope: Scope) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": REQUEST_BODY_TOO_LARGE_DETAIL},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _read_body(receive: Receive, *, max_body_bytes: int) -> bytes:
        chunks: list[bytes] = []
        received_bytes = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            received_bytes += len(chunk)
            if received_bytes > max_body_bytes:
                raise RequestBodyTooLargeError
            chunks.append(chunk)
            more_body = bool(message.get("more_body", False))
        return b"".join(chunks)

    @staticmethod
    def _replay_body(body: bytes) -> Receive:
        sent = False

        async def _receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return _receive


def require_payment(
    *,
    facilitator_url: str,
    bearer_token: str,
    pay_to: str,
    network: str,
    xrp_drops: int | None = None,
    amount: str | None = None,
    asset_code: str = "XRP",
    asset_issuer: str | None = None,
    description: str | None = None,
    mime_type: str = "application/json",
    realm: str | None = None,
) -> RouteConfig:
    if xrp_drops is None and amount is None:
        raise RouteConfigurationError("require_payment needs xrp_drops or amount")
    if xrp_drops is not None and amount is not None:
        raise RouteConfigurationError("require_payment accepts either xrp_drops or amount")

    if asset_code.strip().upper() == "XRP":
        if xrp_drops is None:
            raise RouteConfigurationError("XRP payments must use xrp_drops")
        rendered_amount = str(xrp_drops)
        asset = "XRP:native"
    else:
        if amount is None:
            raise RouteConfigurationError("Issued-asset payments must use amount")
        if asset_issuer is None:
            raise RouteConfigurationError("Issued-asset payments require asset_issuer")
        rendered_amount = amount
        asset = canonical_asset_identifier(
            xrpl_asset_from_identifier(f"{asset_code}:{asset_issuer}")
        )

    option = ChargeRouteSpec(
        network=network,
        recipient=pay_to,
        assetIdentifier=asset,
        amount=rendered_amount,
        description=description,
    )
    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=bearer_token,
        chargeOptions=[option],
        description=description,
        mimeType=mime_type,
        realm=realm,
    )


def require_session(
    *,
    facilitator_url: str,
    bearer_token: str,
    pay_to: str,
    network: str,
    xrp_drops: int | None = None,
    amount: str | None = None,
    min_prepay_amount: str,
    unit_amount: str | None = None,
    asset_code: str = "XRP",
    asset_issuer: str | None = None,
    description: str | None = None,
    mime_type: str = "application/json",
    realm: str | None = None,
    idle_timeout_seconds: int | None = None,
    metering_hints: dict[str, str] | None = None,
) -> RouteConfig:
    if xrp_drops is None and amount is None:
        raise RouteConfigurationError("require_session needs xrp_drops or amount")
    if xrp_drops is not None and amount is not None:
        raise RouteConfigurationError("require_session accepts either xrp_drops or amount")

    if asset_code.strip().upper() == "XRP":
        if xrp_drops is None:
            raise RouteConfigurationError("XRP session routes must use xrp_drops")
        rendered_amount = str(xrp_drops)
        asset = "XRP:native"
    else:
        if amount is None:
            raise RouteConfigurationError("Issued-asset session routes must use amount")
        rendered_amount = amount
        if asset_issuer is None:
            raise RouteConfigurationError("Issued-asset session routes require asset_issuer")
        asset = canonical_asset_identifier(
            xrpl_asset_from_identifier(f"{asset_code}:{asset_issuer}")
        )

    option = SessionRouteSpec(
        network=network,
        recipient=pay_to,
        assetIdentifier=asset,
        amount=rendered_amount,
        minPrepayAmount=min_prepay_amount,
        unitAmount=unit_amount,
        description=description,
        idleTimeoutSeconds=idle_timeout_seconds,
        meteringHints=metering_hints,
    )
    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=bearer_token,
        sessionOptions=[option],
        description=description,
        mimeType=mime_type,
        realm=realm,
    )
