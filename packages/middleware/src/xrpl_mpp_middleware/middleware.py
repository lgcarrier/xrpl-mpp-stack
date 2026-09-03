from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Callable, Mapping, Protocol

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xrpl_mpp_core import (
    AUTHORIZATION_HEADER,
    PAYMENT_AUTHORIZATION_HEADER,
    PAYMENT_RECEIPT_HEADER,
    ChallengeKeyRing,
    IssuedCurrency,
    MPPProblemDetails,
    PaymentChallenge,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_content_digest,
    build_payment_challenge,
    challenge_is_expired,
    decode_session_payload,
    decode_base64url_json,
    encode_payment_receipt,
    parse_accept_payment,
    normalize_currency_code,
    parse_payment_authorization_header,
    payment_credential_header,
    rank_payment_challenges,
    render_payment_challenge,
    serialize_currency,
    verify_challenge_binding,
    xrpl_currency_code,
)
from xrpl_mpp_middleware.client import XRPLFacilitatorClient
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    FacilitatorSettlementPendingError,
    FacilitatorTransportError,
    InvalidPaymentHeaderError,
    RouteConfigurationError,
)
from xrpl_mpp_middleware.types import ChargeRouteSpec, RouteConfig, SessionRouteSpec

WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"
PAYCHANNEL_ID_HEADER = "X-MPP-PayChannel-Id"
PAYCHANNEL_CUMULATIVE_HEADER = "X-MPP-PayChannel-Cumulative"
DEFAULT_MAX_REQUEST_BODY_BYTES = 32_768
REQUEST_BODY_TOO_LARGE_DETAIL = "Request body too large"
MAX_XRPL_UINT64 = (1 << 64) - 1


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
        challenge_secrets: Sequence[str] | None = None,
        challenge_secret: str | None = None,
        challenge_ttl_seconds: int = 300,
        default_realm: str | None = None,
        client_factory: Callable[[str, str], FacilitatorClientProtocol] | None = None,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self.app = app
        if challenge_secrets is not None and challenge_secret is not None:
            raise RouteConfigurationError(
                "Use challenge_secrets, not both challenge_secrets and challenge_secret"
            )
        configured_secrets = challenge_secrets or (() if challenge_secret is None else (challenge_secret,))
        try:
            self._challenge_keys = ChallengeKeyRing(configured_secrets)
        except ValueError as exc:
            raise RouteConfigurationError(str(exc)) from exc
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._default_realm = default_realm.strip() if default_realm else None
        self._client_factory = client_factory
        self._max_request_body_bytes = max_request_body_bytes
        self._startup_lock = asyncio.Lock()
        self._started = False
        self._routes: dict[tuple[str, str], RouteConfig] = {}
        self._clients: dict[tuple[str, str, bool], FacilitatorClientProtocol] = {}

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

        if route_config.session_options:
            try:
                self._validate_paychannel_hints(headers)
            except InvalidPaymentHeaderError:
                await self._send_error(
                    send,
                    receive,
                    scope,
                    400,
                    "Invalid PayChannel session hint headers",
                )
                return

        authorization_values = headers.getlist(AUTHORIZATION_HEADER)
        payment_authorization_values = headers.getlist(PAYMENT_AUTHORIZATION_HEADER)
        if len(authorization_values) > 1 or len(payment_authorization_values) > 1:
            await self._send_error(
                send,
                receive,
                scope,
                400,
                "Duplicate authorization header fields are not allowed",
            )
            return

        await self.startup()

        body_digest = build_content_digest(body)

        authorization = authorization_values[0] if authorization_values else None
        payment_authorization = (
            payment_authorization_values[0]
            if payment_authorization_values
            else None
        )
        credential_fields: list[tuple[str, str]] = []
        if authorization and self._uses_payment_scheme(authorization):
            credential_fields.append((AUTHORIZATION_HEADER, authorization))
        if payment_authorization:
            credential_fields.append((PAYMENT_AUTHORIZATION_HEADER, payment_authorization))

        if len(credential_fields) > 1:
            await self._send_error(
                send,
                receive,
                scope,
                400,
                "Multiple Payment credentials are not allowed",
            )
            return

        if not credential_fields:
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        received_header, raw_credential = credential_fields[0]
        try:
            credential = parse_payment_authorization_header(raw_credential)
        except ValueError as exc:
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error=str(exc),
                problem_code="malformed-credential",
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if payment_credential_header(credential.challenge) != received_header:
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error="Payment credential was sent in a field not selected by its challenge",
                problem_code="invalid-challenge",
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if not verify_challenge_binding(
            credential.challenge,
            secrets=self._challenge_keys.secrets,
        ):
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error="Payment challenge binding is invalid",
                problem_code="invalid-challenge",
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if challenge_is_expired(credential.challenge):
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error="Payment challenge has expired",
                problem_code="payment-expired",
                send=send,
                receive=receive,
                scope=scope,
            )
            return

        if not self._challenge_matches_request(credential.challenge, scope):
            await self._send_challenge(
                route_config,
                headers,
                body_digest,
                error="Payment challenge does not apply to this HTTP operation",
                problem_code="invalid-challenge",
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
                problem_code="verification-failed",
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
            problem_code="verification-failed",
            send=send,
            receive=receive,
            scope=scope,
        )

    @staticmethod
    def _uses_payment_scheme(value: str) -> bool:
        normalized = value.lstrip()
        scheme, separator, _ = normalized.partition(" ")
        if not separator:
            scheme, separator, _ = normalized.partition("\t")
        return bool(separator and scheme.lower() == "payment")

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
        except FacilitatorSettlementPendingError as exc:
            await self._send_facilitator_transport_error(send, receive, scope, exc)
            return
        except FacilitatorTransportError as exc:
            await self._send_facilitator_transport_error(send, receive, scope, exc)
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
            credential_header=payment_credential_header(credential.challenge),
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
        except FacilitatorSettlementPendingError as exc:
            await self._send_facilitator_transport_error(send, receive, scope, exc)
            return
        except FacilitatorTransportError as exc:
            await self._send_facilitator_transport_error(send, receive, scope, exc)
            return
        except FacilitatorProtocolError as exc:
            await self._send_error(send, receive, scope, 502, str(exc))
            return

        payload = decode_session_payload(credential)
        if payload.action == "close":
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
            credential_header=payment_credential_header(credential.challenge),
        )

    async def _forward_paid_request(
        self,
        *,
        receipt: PaymentReceipt,
        body: bytes,
        scope: Scope,
        receive: Receive,
        send: Send,
        credential_header: str,
    ) -> None:
        forward_scope = dict(scope)
        forward_scope["state"] = dict(scope.get("state", {}))
        forward_scope["state"]["mpp_payment"] = receipt
        credential_header_bytes = credential_header.lower().encode("ascii")
        forward_scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != credential_header_bytes
        ]
        response_started = False

        async def send_with_receipt(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                status = int(message.get("status", 500))
                if 200 <= status < 300:
                    headers[PAYMENT_RECEIPT_HEADER] = encode_payment_receipt(receipt)
                    headers["Cache-Control"] = "private"
                elif credential_header == PAYMENT_AUTHORIZATION_HEADER:
                    cache_control = headers.get("Cache-Control", "")
                    if "no-store" not in cache_control.lower():
                        headers["Cache-Control"] = "private"
            await send(message)

        try:
            await self.app(forward_scope, self._replay_body(body), send_with_receipt)
        except Exception:
            if response_started:
                raise
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
        client_key = (
            route_config.facilitator_url,
            route_config.bearer_token,
            route_config.allow_insecure_facilitator_http,
        )
        client = self._clients.get(client_key)
        if client is None:
            if self._client_factory is None:
                client = XRPLFacilitatorClient(
                    base_url=route_config.facilitator_url,
                    bearer_token=route_config.bearer_token,
                    allow_insecure_http=route_config.allow_insecure_facilitator_http,
                )
            else:
                client = self._client_factory(
                    route_config.facilitator_url,
                    route_config.bearer_token,
                )
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

        supported_currencies = set(method_info.currencies)
        supported_intents = set(method_info.intents)

        for option in route_config.charge_options:
            if "charge" not in supported_intents:
                raise RouteConfigurationError("Facilitator does not advertise charge support")
            self._validate_option_network_currency(
                route_key,
                option.network,
                option.currency,
                method_info.network,
                supported_currencies,
            )

        for option in route_config.session_options:
            if "session" not in supported_intents:
                raise RouteConfigurationError("Facilitator does not advertise session support")
            self._validate_option_network_currency(
                route_key,
                option.network,
                option.currency,
                method_info.network,
                supported_currencies,
            )

    @staticmethod
    def _validate_option_network_currency(
        route_key: tuple[str, str],
        network: str,
        currency: str,
        supported_network: str,
        supported_currencies: set[str],
    ) -> None:
        method, path = route_key
        if network != supported_network:
            raise RouteConfigurationError(
                f"{method} {path} expects {network}, but facilitator supports {supported_network}"
            )
        if currency not in supported_currencies:
            raise RouteConfigurationError(
                f"{method} {path} uses unsupported currency {currency}"
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
        problem_code: str | None = None,
    ) -> None:
        challenges = self._build_challenges(route_config, headers, body_digest, scope)
        code = problem_code or ("payment-required" if error is None else "verification-failed")
        titles = {
            "payment-required": "Payment Required",
            "malformed-credential": "Malformed Payment Credential",
            "invalid-challenge": "Invalid Payment Challenge",
            "payment-expired": "Payment Expired",
            "verification-failed": "Payment Verification Failed",
        }
        problem = MPPProblemDetails(
            type=f"https://paymentauth.org/problems/{code}",
            title=titles.get(code, "Payment Required"),
            status=402,
            detail=error or "Payment required for this resource",
            challengeId=challenges[0].id if challenges else None,
        )
        response = JSONResponse(
            status_code=402,
            content=problem.model_dump(by_alias=True, exclude_none=True),
            media_type="application/problem+json",
        )
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
        scope: Scope,
    ) -> list[PaymentChallenge]:
        realm = route_config.realm or self._default_realm or headers.get("host") or "localhost"
        challenges: list[PaymentChallenge] = []
        selected_header = (
            PAYMENT_AUTHORIZATION_HEADER
            if route_config.credential_header == PAYMENT_AUTHORIZATION_HEADER
            else None
        )
        binding = {
            "httpMethod": str(scope["method"]).upper(),
            "path": str(scope["path"]),
            "query": bytes(scope.get("query_string", b"")).decode("latin-1"),
        }

        for option in route_config.charge_options:
            request = XRPLChargeRequest(
                amount=option.amount,
                currency=option.currency,
                recipient=option.recipient,
                description=option.description or route_config.description,
                externalId=option.external_id,
                methodDetails=XRPLChargeMethodDetails(
                    network=option.network,
                ),
            )
            challenges.append(
                build_payment_challenge(
                    secret=self._challenge_keys.active,
                    realm=realm,
                    method="xrpl",
                    intent="charge",
                    request_model=request,
                    expires_in_seconds=self._challenge_ttl_seconds,
                    description=option.description or route_config.description,
                    digest=body_digest,
                    opaque=binding,
                    header=selected_header,
                )
            )

        for option in route_config.session_options:
            channel_id = (headers.get(PAYCHANNEL_ID_HEADER) or option.channel_id).strip().upper()
            cumulative = headers.get(PAYCHANNEL_CUMULATIVE_HEADER)
            request = XRPLSessionRequest(
                amount=option.amount,
                currency="XRP",
                channelId=channel_id,
                recipient=option.recipient,
                description=option.description or route_config.description,
                externalId=option.external_id,
                methodDetails=XRPLSessionMethodDetails(
                    network=option.network,
                    cumulativeAmount=cumulative,
                ),
            )
            challenges.append(
                build_payment_challenge(
                    secret=self._challenge_keys.active,
                    realm=realm,
                    method="xrpl",
                    intent="session",
                    request_model=request,
                    expires_in_seconds=self._challenge_ttl_seconds,
                    description=option.description or route_config.description,
                    digest=body_digest,
                    opaque=binding,
                    header=selected_header,
                )
            )
        accept_payment = headers.get("Accept-Payment")
        if accept_payment:
            try:
                ranked = rank_payment_challenges(
                    challenges,
                    parse_accept_payment(accept_payment),
                )
            except ValueError:
                ranked = []
            if ranked:
                return ranked
        return challenges

    @staticmethod
    def _validate_paychannel_hints(headers: Headers) -> None:
        channel_values = headers.getlist(PAYCHANNEL_ID_HEADER)
        cumulative_values = headers.getlist(PAYCHANNEL_CUMULATIVE_HEADER)
        if len(channel_values) > 1 or len(cumulative_values) > 1:
            raise InvalidPaymentHeaderError("PayChannel hint headers must be unique")
        channel_id = channel_values[0] if channel_values else None
        cumulative = cumulative_values[0] if cumulative_values else None
        if channel_id is None and cumulative is None:
            return
        if channel_id is None or cumulative is None:
            raise InvalidPaymentHeaderError(
                "PayChannel ID and cumulative hint headers must be provided together"
            )
        if (
            channel_id != channel_id.strip()
            or len(channel_id) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in channel_id)
        ):
            raise InvalidPaymentHeaderError("PayChannel ID hint is invalid")
        if (
            cumulative != cumulative.strip()
            or not cumulative
            or not cumulative.isascii()
            or not cumulative.isdigit()
            or len(cumulative) > 20
            or int(cumulative) > MAX_XRPL_UINT64
        ):
            raise InvalidPaymentHeaderError("PayChannel cumulative hint is invalid")

    @staticmethod
    def _challenge_matches_request(challenge: PaymentChallenge, scope: Scope) -> bool:
        if not challenge.opaque:
            return False
        try:
            binding = decode_base64url_json(challenge.opaque)
        except ValueError:
            return False
        return binding == {
            "httpMethod": str(scope["method"]).upper(),
            "path": str(scope["path"]),
            "query": bytes(scope.get("query_string", b"")).decode("latin-1"),
        }

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
    async def _send_facilitator_transport_error(
        send: Send,
        receive: Receive,
        scope: Scope,
        error: FacilitatorTransportError,
    ) -> None:
        headers = {"Cache-Control": "private, no-store"}
        if isinstance(error, FacilitatorSettlementPendingError):
            problem = error.problem
            if error.retry_after is not None:
                headers["Retry-After"] = error.retry_after
        else:
            problem = MPPProblemDetails(
                type="https://paymentauth.org/problems/settlement-unknown",
                title="Payment settlement status unknown",
                status=503,
                detail=error.detail,
                paymentReference=error.payment_reference,
            )
        response = JSONResponse(
            status_code=503,
            content=problem.model_dump(by_alias=True, exclude_none=True),
            media_type="application/problem+json",
            headers=headers,
        )
        await response(scope, receive, send)

    @staticmethod
    async def _send_paid_internal_error(
        send: Send,
        receive: Receive,
        scope: Scope,
        receipt: PaymentReceipt,
    ) -> None:
        problem = MPPProblemDetails(
            type="https://paymentauth.org/problems/protected-resource-failed",
            title="Protected Resource Failed",
            status=500,
            detail="The protected application failed after payment settlement",
            paymentReference=receipt.reference,
        )
        response = JSONResponse(
            status_code=500,
            content=problem.model_dump(by_alias=True, exclude_none=True),
        )
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
    allow_insecure_facilitator_http: bool = False,
) -> RouteConfig:
    if xrp_drops is None and amount is None:
        raise RouteConfigurationError("require_payment needs xrp_drops or amount")
    if xrp_drops is not None and amount is not None:
        raise RouteConfigurationError("require_payment accepts either xrp_drops or amount")

    if asset_code.strip().upper() == "XRP":
        if xrp_drops is None:
            raise RouteConfigurationError("XRP payments must use xrp_drops")
        rendered_amount = str(xrp_drops)
        currency = "XRP"
    else:
        if amount is None:
            raise RouteConfigurationError("Issued-asset payments must use amount")
        if asset_issuer is None:
            raise RouteConfigurationError("Issued-asset payments require asset_issuer")
        rendered_amount = amount
        currency = serialize_currency(
            IssuedCurrency(
                currency=xrpl_currency_code(normalize_currency_code(asset_code)),
                issuer=asset_issuer,
            )
        )

    option = ChargeRouteSpec(
        network=network,
        recipient=pay_to,
        currency=currency,
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
        allowInsecureFacilitatorHttp=allow_insecure_facilitator_http,
    )


def require_session(
    *,
    facilitator_url: str,
    bearer_token: str,
    pay_to: str,
    network: str,
    xrp_drops: int,
    channel_id: str = "",
    description: str | None = None,
    mime_type: str = "application/json",
    realm: str | None = None,
    allow_insecure_facilitator_http: bool = False,
) -> RouteConfig:
    if xrp_drops < 0:
        raise RouteConfigurationError("xrp_drops must be zero or greater")

    option = SessionRouteSpec(
        network=network,
        recipient=pay_to,
        currency="XRP",
        amount=str(xrp_drops),
        channelId=channel_id,
        description=description,
    )
    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=bearer_token,
        sessionOptions=[option],
        description=description,
        mimeType=mime_type,
        realm=realm,
        allowInsecureFacilitatorHttp=allow_insecure_facilitator_http,
    )
