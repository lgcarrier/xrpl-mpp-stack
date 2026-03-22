from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from xrpl_mpp_core import PaymentChallenge, decode_challenge_request
from xrpl_mpp_client.signer import (
    AUTHORIZATION_HEADER,
    PAYMENT_RECEIPT_HEADER,
    SESSION_ID_HEADER,
    WWW_AUTHENTICATE_HEADER,
    XRPLPaymentSigner,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)


@dataclass
class SessionState:
    session_id: str
    session_token: str
    request_method: str
    recipient: str | None = None
    network: str | None = None
    asset_identifier: str | None = None
    amount: str | None = None
    min_prepay_amount: str | None = None


class XRPLPaymentTransport(httpx.AsyncBaseTransport):
    RETRY_KEY = "_xrpl_mpp_retry"

    def __init__(
        self,
        signer: XRPLPaymentSigner,
        *,
        network: str | None = None,
        asset: str | None = None,
        base_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._signer = signer
        self._network = network
        self._asset = asset
        self._base_transport = base_transport or httpx.AsyncHTTPTransport()
        self._sessions: dict[str, SessionState] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_key = self._session_key(request)
        existing_session = self._sessions.get(request_key)
        initial_headers = request.headers.copy()
        if existing_session is not None:
            initial_headers[SESSION_ID_HEADER] = existing_session.session_id
        initial_request = self._clone_request(request, headers=initial_headers)
        response = await self._base_transport.handle_async_request(initial_request)
        await response.aread()
        if response.status_code != 402 or request.extensions.get(self.RETRY_KEY):
            self._capture_session_receipt(request_key, response)
            return response

        challenges = decode_payment_challenges_response(response.headers)
        if not challenges:
            return response

        challenge = self._select_challenge(
            challenges,
            existing_session is not None,
            session_state=existing_session,
        )

        if challenge.intent == "charge":
            credential = await self._signer.build_charge_credential_async(challenge)
            retry_headers = initial_headers.copy()
            retry_headers[AUTHORIZATION_HEADER] = build_payment_authorization(credential)
            retry_request = self._clone_request(
                request,
                headers=retry_headers,
                extensions={self.RETRY_KEY: True},
            )
            retry_response = await self._base_transport.handle_async_request(retry_request)
            await retry_response.aread()
            self._capture_session_receipt(request_key, retry_response)
            return retry_response

        if existing_session is None or existing_session.session_id != self._session_id_from_challenge(challenge):
            credential = await self._signer.build_session_open_credential_async(challenge)
            open_headers = initial_headers.copy()
            open_headers[SESSION_ID_HEADER] = self._session_id_from_challenge(challenge)
            open_headers[AUTHORIZATION_HEADER] = build_payment_authorization(credential)
            open_request = self._clone_request(
                request,
                headers=open_headers,
                extensions={self.RETRY_KEY: True},
            )
            open_response = await self._base_transport.handle_async_request(open_request)
            await open_response.aread()
            self._capture_session_receipt(
                request_key,
                open_response,
                challenge=challenge,
                request_method=request.method.upper(),
            )
            return open_response

        use_headers = initial_headers.copy()
        use_headers[SESSION_ID_HEADER] = existing_session.session_id
        use_credential = await self._signer.build_session_use_credential_async(
            challenge,
            session_token=existing_session.session_token,
        )
        use_headers[AUTHORIZATION_HEADER] = build_payment_authorization(use_credential)
        use_request = self._clone_request(
            request,
            headers=use_headers,
            extensions={self.RETRY_KEY: True},
        )
        use_response = await self._base_transport.handle_async_request(use_request)
        await use_response.aread()
        if use_response.status_code != 402:
            self._capture_session_receipt(request_key, use_response, session_state=existing_session)
            return use_response

        top_up_challenges = decode_payment_challenges_response(use_response.headers)
        if not top_up_challenges:
            return use_response
        top_up_challenge = self._select_challenge(
            top_up_challenges,
            prefer_session=True,
            session_state=existing_session,
        )
        top_up_headers = initial_headers.copy()
        top_up_headers[SESSION_ID_HEADER] = existing_session.session_id
        top_up_credential = await self._signer.build_session_top_up_credential_async(
            top_up_challenge,
            session_token=existing_session.session_token,
        )
        top_up_headers[AUTHORIZATION_HEADER] = build_payment_authorization(top_up_credential)
        top_up_request = self._clone_request(
            request,
            headers=top_up_headers,
            extensions={self.RETRY_KEY: True},
        )
        top_up_response = await self._base_transport.handle_async_request(top_up_request)
        await top_up_response.aread()
        self._capture_session_receipt(request_key, top_up_response, session_state=existing_session)
        if top_up_response.status_code >= 400:
            return top_up_response

        final_headers = initial_headers.copy()
        final_headers[SESSION_ID_HEADER] = existing_session.session_id
        final_challenges = decode_payment_challenges_response(top_up_response.headers)
        if final_challenges:
            final_challenge = self._select_challenge(
                final_challenges,
                prefer_session=True,
                session_state=existing_session,
            )
        else:
            final_402 = await self._base_transport.handle_async_request(
                self._clone_request(request, headers=final_headers)
            )
            await final_402.aread()
            parsed = decode_payment_challenges_response(final_402.headers)
            if not parsed:
                return final_402
            final_challenge = self._select_challenge(
                parsed,
                prefer_session=True,
                session_state=existing_session,
            )

        final_credential = await self._signer.build_session_use_credential_async(
            final_challenge,
            session_token=existing_session.session_token,
        )
        final_headers[AUTHORIZATION_HEADER] = build_payment_authorization(final_credential)
        final_request = self._clone_request(
            request,
            headers=final_headers,
            extensions={self.RETRY_KEY: True},
        )
        final_response = await self._base_transport.handle_async_request(final_request)
        await final_response.aread()
        self._capture_session_receipt(request_key, final_response, session_state=existing_session)
        return final_response

    async def close_session(
        self,
        url: str,
        *,
        method: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_key = self._session_key_from_url(url, method=method)
        existing_session = self._sessions.get(request_key)
        if existing_session is None:
            raise ValueError(f"No active MPP session for {request_key}")
        request = httpx.Request(
            method or existing_session.request_method,
            url,
            headers=headers or {},
        )

        initial_headers = request.headers.copy()
        initial_headers[SESSION_ID_HEADER] = existing_session.session_id
        initial_request = self._clone_request(request, headers=initial_headers)
        response = await self._base_transport.handle_async_request(initial_request)
        await response.aread()
        if response.status_code != 402:
            self._capture_session_receipt(request_key, response)
            return response

        challenges = decode_payment_challenges_response(response.headers)
        if not challenges:
            return response

        challenge = self._select_challenge(
            challenges,
            prefer_session=True,
            session_state=existing_session,
        )
        credential = await self._signer.build_session_close_credential_async(
            challenge,
            session_token=existing_session.session_token,
        )
        retry_headers = initial_headers.copy()
        retry_headers[AUTHORIZATION_HEADER] = build_payment_authorization(credential)
        retry_request = self._clone_request(
            request,
            headers=retry_headers,
            extensions={self.RETRY_KEY: True},
        )
        retry_response = await self._base_transport.handle_async_request(retry_request)
        await retry_response.aread()
        self._capture_session_receipt(request_key, retry_response, session_state=existing_session)
        return retry_response

    async def aclose(self) -> None:
        await self._base_transport.aclose()

    def _select_challenge(
        self,
        challenges: list[PaymentChallenge],
        prefer_session: bool,
        *,
        session_state: SessionState | None = None,
    ) -> PaymentChallenge:
        if prefer_session:
            if session_state is not None:
                selected = self._select_session_challenge(challenges, session_state=session_state)
                if selected is not None:
                    return selected
            try:
                return select_payment_challenge(
                    challenges,
                    intent="session",
                    network=self._network,
                    asset=self._asset,
                )
            except ValueError:
                pass
        return select_payment_challenge(
            challenges,
            network=self._network,
            asset=self._asset,
        )

    def _select_session_challenge(
        self,
        challenges: list[PaymentChallenge],
        *,
        session_state: SessionState,
    ) -> PaymentChallenge | None:
        for challenge in challenges:
            if challenge.method != "xrpl" or challenge.intent != "session":
                continue
            try:
                request = decode_challenge_request(challenge)
            except ValueError:
                continue
            if request.method_details.session_id != session_state.session_id:
                continue
            if session_state.recipient is not None and request.recipient != session_state.recipient:
                continue
            if session_state.network is not None and request.method_details.network != session_state.network:
                continue
            if session_state.asset_identifier is not None and request.currency != session_state.asset_identifier:
                continue
            if session_state.amount is not None and request.amount != session_state.amount:
                continue
            if (
                session_state.min_prepay_amount is not None
                and request.method_details.min_prepay_amount != session_state.min_prepay_amount
            ):
                continue
            return challenge
        return None

    def _capture_session_receipt(
        self,
        request_key: str,
        response: httpx.Response,
        *,
        challenge: PaymentChallenge | None = None,
        request_method: str | None = None,
        session_state: SessionState | None = None,
    ) -> None:
        receipt = decode_payment_receipt_header(response.headers)
        if receipt is None:
            return
        if receipt.intent == "session" and receipt.session_id and receipt.session_token:
            active_state = session_state
            if challenge is not None:
                request = decode_challenge_request(challenge)
                recipient = request.recipient
                network = request.method_details.network
                asset_identifier = request.currency
                amount = request.amount
                min_prepay_amount = request.method_details.min_prepay_amount
            else:
                recipient = active_state.recipient if active_state is not None else None
                network = active_state.network if active_state is not None else None
                asset_identifier = active_state.asset_identifier if active_state is not None else None
                amount = active_state.amount if active_state is not None else None
                min_prepay_amount = (
                    active_state.min_prepay_amount if active_state is not None else None
                )
            self._sessions[request_key] = SessionState(
                session_id=receipt.session_id,
                session_token=receipt.session_token,
                request_method=request_method or response.request.method.upper(),
                recipient=recipient,
                network=network,
                asset_identifier=asset_identifier,
                amount=amount,
                min_prepay_amount=min_prepay_amount,
            )
        if receipt.intent == "session" and receipt.last_action == "close":
            self._sessions.pop(request_key, None)

    @staticmethod
    def _clone_request(
        request: httpx.Request,
        *,
        method: str | None = None,
        headers: httpx.Headers | dict[str, str] | None = None,
        content: bytes | None = None,
        extensions: dict[str, object] | None = None,
    ) -> httpx.Request:
        cloned_extensions = dict(request.extensions)
        if extensions:
            cloned_extensions.update(extensions)
        return httpx.Request(
            method=method or request.method,
            url=request.url,
            headers=headers or request.headers,
            content=request.content if content is None else content,
            extensions=cloned_extensions,
        )

    @staticmethod
    def _session_resource_key(url: str) -> str:
        parts = urlsplit(url)
        resource = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if parts.query:
            resource = f"{resource}?{parts.query}"
        return resource

    @staticmethod
    def _session_cache_key(method: str, resource_key: str) -> str:
        return f"{method.upper()} {resource_key}"

    @classmethod
    def _session_key(cls, request: httpx.Request) -> str:
        return cls._session_cache_key(
            request.method.upper(),
            cls._session_resource_key(str(request.url)),
        )

    def _session_key_from_url(self, url: str, *, method: str | None = None) -> str:
        resource_key = self._session_resource_key(url)
        if method is not None:
            return self._session_cache_key(method, resource_key)

        matches = [
            key
            for key in self._matching_session_keys(resource_key, tuple(self._sessions))
        ]
        if not matches:
            return self._session_cache_key("GET", resource_key)
        if len(matches) > 1:
            raise ValueError(
                f"Multiple active MPP sessions for {resource_key}; specify method to close the intended session"
            )
        return matches[0]

    @staticmethod
    def _matching_session_keys(resource_key: str, session_keys: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            key
            for key in session_keys
            if key.partition(" ")[2] == resource_key
        )

    @staticmethod
    def _session_id_from_challenge(challenge: PaymentChallenge) -> str:
        return decode_challenge_request(challenge).method_details.session_id


def wrap_httpx_with_mpp_payment(
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    payment_transport = XRPLPaymentTransport(
        signer,
        network=network,
        asset=asset,
        base_transport=transport,
    )
    return httpx.AsyncClient(base_url=base_url or "", timeout=timeout, transport=payment_transport)
