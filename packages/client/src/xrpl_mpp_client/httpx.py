from __future__ import annotations

from dataclasses import dataclass
import hmac
from urllib.parse import urlsplit

import httpx
from xrpl.core import binarycodec
from xrpl.models.transactions import Payment, Transaction

from xrpl_mpp_core import (
    ACCEPT_PAYMENT_HEADER,
    PAYMENT_AUTHORIZATION_HEADER,
    AcceptPaymentRange,
    PaymentChallenge,
    PaymentCredential,
    XRPLNetwork,
    build_content_digest,
    challenge_invoice_id,
    challenge_is_expired,
    decode_charge_payload,
    decode_challenge_request,
    decode_session_payload,
    payment_credential_header,
    rank_payment_challenges,
    render_accept_payment,
)
from xrpl_mpp_client.signer import (
    AUTHORIZATION_HEADER,
    XRPLPaymentSigner,
    build_payment_authorization,
    derive_paychannel_open_binding,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)
from xrpl_mpp_client.policy import PaymentPolicyError, XRPLPaymentPolicy

PAYCHANNEL_ID_HEADER = "X-MPP-PayChannel-Id"
PAYCHANNEL_CUMULATIVE_HEADER = "X-MPP-PayChannel-Cumulative"


class PaymentRequestBindingError(ValueError):
    """Raised before signing when a challenge is bound to a different body."""


@dataclass(frozen=True)
class PayChannelSessionState:
    channel_id: str
    cumulative_amount: str
    request_method: str
    recipient: str | None = None
    network: XRPLNetwork | None = None


class XRPLPaymentTransport(httpx.AsyncBaseTransport):
    """One-retry HTTPX transport for charge and cumulative PayChannel proofs."""

    RETRY_KEY = "_xrpl_mpp_retry"
    CREDENTIAL_REQUEST_KEY = "_xrpl_mpp_credential_request_key"

    def __init__(
        self,
        signer: XRPLPaymentSigner,
        *,
        network: XRPLNetwork | None = None,
        currency: str | None = None,
        base_transport: httpx.AsyncBaseTransport | None = None,
        payment_preferences: list[AcceptPaymentRange] | None = None,
        payment_policy: XRPLPaymentPolicy | None = None,
        allow_insecure_localhost: bool = False,
    ) -> None:
        self._signer = signer
        self._network = network
        self._currency = currency
        self._base_transport = base_transport or httpx.AsyncHTTPTransport()
        self._payment_preferences = payment_preferences or [
            AcceptPaymentRange(method="xrpl", intent="charge"),
            AcceptPaymentRange(method="xrpl", intent="session"),
        ]
        self._payment_policy = payment_policy or signer.automatic_payment_policy
        self._allow_insecure_localhost = allow_insecure_localhost
        self._sessions: dict[str, PayChannelSessionState] = {}
        self._open_transactions: dict[str, str] = {}

    def register_channel(
        self,
        url: str,
        *,
        channel_id: str,
        cumulative_amount: str = "0",
        method: str = "GET",
        recipient: str | None = None,
        network: XRPLNetwork | None = None,
    ) -> None:
        """Resume a known on-ledger channel for one HTTP resource."""

        if len(channel_id) != 64 or any(char not in "0123456789abcdefABCDEF" for char in channel_id):
            raise ValueError("channel_id must be 64 hexadecimal characters")
        if not cumulative_amount.isascii() or not cumulative_amount.isdigit():
            raise ValueError("cumulative_amount must be an unsigned drops string")
        key = self._session_key_from_url(url, method=method)
        self._sessions[key] = PayChannelSessionState(
            channel_id=channel_id.upper(),
            cumulative_amount=cumulative_amount,
            request_method=method.upper(),
            recipient=recipient,
            network=network,
        )

    def register_open_transaction(
        self,
        url: str,
        *,
        transaction: str,
        method: str = "GET",
    ) -> None:
        """Supply a signed ``PaymentChannelCreate`` blob for an open challenge."""

        if not transaction or any(char not in "0123456789abcdefABCDEF" for char in transaction):
            raise ValueError("transaction must be a hexadecimal transaction blob")
        self._open_transactions[self._session_key_from_url(url, method=method)] = transaction

    def channel_state(
        self,
        url: str,
        *,
        method: str = "GET",
    ) -> PayChannelSessionState | None:
        """Return the verified local PayChannel state for one HTTP resource."""

        return self._sessions.get(self._session_key_from_url(url, method=method))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._require_secure_transport(request.url)
        body = await request.aread()
        request_key = self._session_key(request)
        initial_headers = request.headers.copy()
        credential_request_key = request.extensions.get(self.CREDENTIAL_REQUEST_KEY)
        if credential_request_key is not None and credential_request_key != request_key:
            # HTTPX deliberately preserves custom headers and request extensions
            # while following redirects. Payment credentials are bound to the
            # original method and resource, so never forward one to a redirected
            # request (cross-origin or otherwise). Preserve unrelated Bearer auth.
            initial_headers.pop(PAYMENT_AUTHORIZATION_HEADER, None)
            authorization = initial_headers.get(AUTHORIZATION_HEADER)
            if authorization is not None and authorization.startswith("Payment "):
                initial_headers.pop(AUTHORIZATION_HEADER, None)
        if ACCEPT_PAYMENT_HEADER not in initial_headers:
            initial_headers[ACCEPT_PAYMENT_HEADER] = render_accept_payment(self._payment_preferences)
        session_state = self._sessions.get(request_key)
        if session_state is not None:
            initial_headers[PAYCHANNEL_ID_HEADER] = session_state.channel_id
            initial_headers[PAYCHANNEL_CUMULATIVE_HEADER] = session_state.cumulative_amount

        response = await self._send(request, headers=initial_headers)
        if response.status_code != 402 or request.extensions.get(self.RETRY_KEY):
            return response

        challenges = decode_payment_challenges_response(response.headers)
        if not challenges:
            return response
        try:
            challenge = self._select_challenge(challenges, request_key=request_key)
        except ValueError:
            return response
        if challenge_is_expired(challenge):
            raise ValueError("Refusing to authorize an expired MPP challenge")
        self._verify_challenge_digest(challenge, body)
        self._authorize_automatic_payment(challenge)

        if challenge.intent == "charge":
            credential = await self._signer.build_charge_credential_async(challenge)
        elif challenge.intent == "session":
            credential = await self._session_credential(challenge, request_key=request_key)
        else:
            return response

        retry_headers = initial_headers.copy()
        self._apply_credential(retry_headers, credential)
        retry_response = await self._send(
            request,
            headers=retry_headers,
            extensions={
                self.RETRY_KEY: True,
                self.CREDENTIAL_REQUEST_KEY: request_key,
            },
        )
        if 200 <= retry_response.status_code < 300:
            if challenge.intent == "charge":
                self._validate_charge_success(challenge, credential, retry_response)
            elif challenge.intent == "session":
                self._capture_session_success(
                    request_key,
                    challenge,
                    credential,
                    retry_response,
                    request_method=request.method,
                )
        return retry_response

    async def close_session(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_key = self._session_key_from_url(url, method=method)
        state = self._sessions.get(request_key)
        if state is None:
            raise ValueError(f"No active MPP PayChannel for {request_key}")

        request = httpx.Request(method, url, headers=headers or {})
        self._require_secure_transport(request.url)
        initial_headers = request.headers.copy()
        initial_headers[ACCEPT_PAYMENT_HEADER] = render_accept_payment(
            [AcceptPaymentRange(method="xrpl", intent="session")]
        )
        initial_headers[PAYCHANNEL_ID_HEADER] = state.channel_id
        initial_headers[PAYCHANNEL_CUMULATIVE_HEADER] = state.cumulative_amount
        response = await self._send(request, headers=initial_headers)
        if response.status_code != 402:
            return response
        challenges = decode_payment_challenges_response(response.headers)
        challenge = self._matching_session_challenge(challenges, state)
        if challenge is None:
            return response
        if challenge_is_expired(challenge):
            raise ValueError("Refusing to authorize an expired MPP challenge")
        self._verify_challenge_digest(challenge, request.content)
        self._authorize_automatic_payment(challenge)
        terms = self._require_matching_session_state(challenge, state)
        cumulative = str(int(state.cumulative_amount) + int(terms.amount))
        credential = await self._signer.build_session_close_credential_async(
            challenge,
            cumulative_amount=cumulative,
        )
        retry_headers = initial_headers.copy()
        self._apply_credential(retry_headers, credential)
        retry_response = await self._send(
            request,
            headers=retry_headers,
            extensions={
                self.RETRY_KEY: True,
                self.CREDENTIAL_REQUEST_KEY: request_key,
            },
        )
        if 200 <= retry_response.status_code < 300:
            self._capture_session_success(
                request_key,
                challenge,
                credential,
                retry_response,
                request_method=method,
            )
        return retry_response

    async def aclose(self) -> None:
        await self._base_transport.aclose()

    def _authorize_automatic_payment(self, challenge: PaymentChallenge) -> None:
        if self._payment_policy is None:
            raise PaymentPolicyError(
                "Automatic XRPL payment is disabled without a complete payment policy; "
                "configure recipient, max amount, and allowed currencies"
            )
        self._payment_policy.authorize(challenge)

    @staticmethod
    def _verify_challenge_digest(challenge: PaymentChallenge, body: bytes) -> None:
        if challenge.digest is None:
            return
        actual = build_content_digest(body)
        if actual is None or not hmac.compare_digest(challenge.digest, actual):
            raise PaymentRequestBindingError(
                "Payment challenge digest does not match the request body"
            )

    async def _session_credential(
        self,
        challenge: PaymentChallenge,
        *,
        request_key: str,
    ) -> PaymentCredential:
        terms = decode_challenge_request(challenge)
        if terms.channel_id:
            state = self._sessions.get(request_key)
            if state is None:
                raise ValueError(
                    "automatic PayChannel vouchers require registered local channel state"
                )
            terms = self._require_matching_session_state(challenge, state)
            cumulative = str(int(state.cumulative_amount) + int(terms.amount))
            return await self._signer.build_session_voucher_credential_async(
                challenge,
                cumulative_amount=cumulative,
            )

        open_transaction = self._open_transactions.get(request_key)
        if open_transaction is None:
            raise ValueError(
                "session challenge requires a signed PaymentChannelCreate blob; "
                "call register_open_transaction() first"
            )
        return await self._signer.build_session_open_credential_async(
            challenge,
            open_transaction=open_transaction,
        )

    def _capture_session_success(
        self,
        request_key: str,
        challenge: PaymentChallenge,
        credential: PaymentCredential,
        response: httpx.Response,
        *,
        request_method: str,
    ) -> None:
        payload = decode_session_payload(credential)
        terms = decode_challenge_request(challenge)
        receipt = decode_payment_receipt_header(response.headers)
        if receipt is None:
            return

        if payload.action == "open":
            binding = derive_paychannel_open_binding(payload.transaction)
            channel_id = binding.channel_id
            expected_reference = f"open:{binding.channel_id}:{binding.tx_hash}"
            expected_payer = binding.payer
            expected_recipient = binding.recipient
            expected_tx_hash = binding.tx_hash
        else:
            channel_id = payload.channel_id.upper()
            expected_reference = f"{channel_id}:{payload.amount}"
            expected_payer = self._signer.wallet.classic_address
            expected_recipient = terms.recipient
            expected_tx_hash = None

        expected_network = (
            terms.method_details.network if terms.method_details else self._network
        )
        if receipt.method != challenge.method:
            raise ValueError("Payment-Receipt method does not match the session challenge")
        if receipt.reference != expected_reference:
            raise ValueError(
                "Payment-Receipt reference does not match the signed session credential"
            )
        if receipt.challenge_id is not None and receipt.challenge_id != challenge.id:
            raise ValueError(
                "Payment-Receipt challengeId does not match the session challenge"
            )
        receipt_intent = (receipt.model_extra or {}).get("intent")
        if receipt_intent is not None and receipt_intent != challenge.intent:
            raise ValueError("Payment-Receipt intent does not match the session challenge")
        receipt_external_id = (receipt.model_extra or {}).get("externalId")
        if receipt_external_id is not None and receipt_external_id != challenge.id:
            raise ValueError(
                "Payment-Receipt externalId does not match the session challenge ID"
            )
        if receipt.action is not None and receipt.action != payload.action:
            raise ValueError("Payment-Receipt action does not match the session credential")
        if receipt.channel_id is not None and receipt.channel_id.upper() != channel_id:
            raise ValueError(
                "Payment-Receipt channelId does not match the signed session credential"
            )
        if receipt.cumulative is not None and receipt.cumulative != payload.amount:
            raise ValueError(
                "Payment-Receipt cumulative does not match the signed session credential"
            )
        if receipt.payer is not None and receipt.payer != expected_payer:
            raise ValueError(
                "Payment-Receipt payer does not match the signed session credential"
            )
        if receipt.recipient is not None and receipt.recipient != expected_recipient:
            raise ValueError(
                "Payment-Receipt recipient does not match the session challenge"
            )
        if (
            expected_network is not None
            and receipt.network is not None
            and receipt.network != expected_network
        ):
            raise ValueError("Payment-Receipt network does not match the session challenge")
        if (
            receipt.tx_hash is not None
            and expected_tx_hash is not None
            and receipt.tx_hash.upper() != expected_tx_hash
        ):
            raise ValueError(
                "Payment-Receipt txHash does not match the signed open transaction"
            )

        if payload.action == "open":
            self._open_transactions.pop(request_key, None)
        elif payload.action == "close":
            self._sessions.pop(request_key, None)
            return
        else:
            cumulative = payload.amount
        self._sessions[request_key] = PayChannelSessionState(
            channel_id=channel_id,
            cumulative_amount=payload.amount,
            request_method=request_method.upper(),
            recipient=terms.recipient,
            network=terms.method_details.network if terms.method_details else None,
        )

    def _validate_charge_success(
        self,
        challenge: PaymentChallenge,
        credential: PaymentCredential,
        response: httpx.Response,
    ) -> None:
        """Bind a successful charge receipt to the credential that was sent."""

        receipt = decode_payment_receipt_header(response.headers)
        if receipt is None:
            return

        payload = decode_charge_payload(credential)
        if payload.type == "transaction":
            try:
                decoded = binarycodec.decode(payload.blob)
                transaction = Transaction.from_xrpl(decoded)
            except Exception as exc:
                raise ValueError(
                    "signed charge credential contains an invalid XRPL transaction"
                ) from exc
            if not isinstance(transaction, Payment):
                raise ValueError("signed charge credential is not an XRPL Payment")
            expected_reference = transaction.get_hash().upper()
            expected_payer = transaction.account
        else:
            expected_reference = payload.hash.upper()
            expected_payer = self._signer.wallet.classic_address

        terms = decode_challenge_request(challenge)
        details = terms.method_details
        expected_network = details.network if details is not None else self._network
        expected_invoice_id = (
            details.invoice_id
            if details is not None and details.invoice_id is not None
            else challenge_invoice_id(challenge.id)
        )

        if receipt.method != challenge.method:
            raise ValueError("Payment-Receipt method does not match the charge challenge")
        if receipt.reference != expected_reference:
            raise ValueError(
                "Payment-Receipt reference does not match the signed charge credential"
            )
        if receipt.challenge_id is not None and receipt.challenge_id != challenge.id:
            raise ValueError(
                "Payment-Receipt challengeId does not match the charge challenge"
            )
        receipt_extra = receipt.model_extra or {}
        receipt_intent = receipt_extra.get("intent")
        if receipt_intent is not None and receipt_intent != challenge.intent:
            raise ValueError("Payment-Receipt intent does not match the charge challenge")
        receipt_external_id = receipt_extra.get("externalId")
        if receipt_external_id is not None and receipt_external_id != challenge.id:
            raise ValueError(
                "Payment-Receipt externalId does not match the charge challenge ID"
            )
        if receipt.network is not None and receipt.network != expected_network:
            raise ValueError("Payment-Receipt network does not match the charge challenge")
        if receipt.payer is not None and receipt.payer != expected_payer:
            raise ValueError(
                "Payment-Receipt payer does not match the signed charge credential"
            )
        if receipt.recipient is not None and receipt.recipient != terms.recipient:
            raise ValueError(
                "Payment-Receipt recipient does not match the charge challenge"
            )
        if (
            receipt.invoice_id is not None
            and receipt.invoice_id.upper() != expected_invoice_id.upper()
        ):
            raise ValueError(
                "Payment-Receipt invoiceId does not match the charge challenge"
            )
        if receipt.tx_hash is not None and receipt.tx_hash.upper() != expected_reference:
            raise ValueError(
                "Payment-Receipt txHash does not match the signed charge credential"
            )
        if receipt.action is not None:
            raise ValueError("Payment-Receipt action is not valid for a charge receipt")
        if receipt.channel_id is not None:
            raise ValueError("Payment-Receipt channelId is not valid for a charge receipt")
        if receipt.cumulative is not None:
            raise ValueError("Payment-Receipt cumulative is not valid for a charge receipt")

    def _select_challenge(
        self,
        challenges: list[PaymentChallenge],
        *,
        request_key: str,
    ) -> PaymentChallenge:
        candidates = rank_payment_challenges(challenges, self._payment_preferences)
        if not candidates:
            raise ValueError("No acceptable MPP challenge found")
        state = self._sessions.get(request_key)
        if state is not None:
            matching = self._matching_session_challenge(candidates, state)
            if matching is not None:
                return matching
        return select_payment_challenge(
            candidates,
            network=self._network,
            currency=self._currency,
        )

    @classmethod
    def _matching_session_challenge(
        cls,
        challenges: list[PaymentChallenge],
        state: PayChannelSessionState,
    ) -> PaymentChallenge | None:
        for challenge in challenges:
            if challenge.method != "xrpl" or challenge.intent != "session":
                continue
            try:
                cls._require_matching_session_state(challenge, state)
            except ValueError:
                continue
            return challenge
        return None

    @staticmethod
    def _require_matching_session_state(
        challenge: PaymentChallenge,
        state: PayChannelSessionState,
    ):
        """Bind unattended session signing to the client's durable high-water mark."""

        terms = decode_challenge_request(challenge)
        if terms.channel_id.upper() != state.channel_id.upper():
            raise ValueError("server challenge channelId does not match registered local state")
        if state.recipient is not None and terms.recipient != state.recipient:
            raise ValueError("server challenge recipient does not match registered local state")
        if (
            state.network is not None
            and terms.method_details is not None
            and terms.method_details.network not in {None, state.network}
        ):
            raise ValueError("server challenge network does not match registered local state")

        details = terms.method_details
        challenge_cumulative = (
            details.cumulative_amount if details is not None else None
        )
        if challenge_cumulative is None:
            raise ValueError(
                "server session challenge is missing methodDetails.cumulativeAmount"
            )
        if int(challenge_cumulative) != int(state.cumulative_amount):
            raise ValueError(
                "server challenge methodDetails.cumulativeAmount does not match "
                "registered local state"
            )
        return terms

    async def _send(
        self,
        request: httpx.Request,
        *,
        headers: httpx.Headers | dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        cloned = self._clone_request(request, headers=headers, extensions=extensions)
        response = await self._base_transport.handle_async_request(cloned)
        await response.aread()
        return response

    @staticmethod
    def _apply_credential(headers: httpx.Headers, credential: PaymentCredential) -> None:
        selected_header = payment_credential_header(credential.challenge)
        if selected_header == AUTHORIZATION_HEADER:
            headers.pop(PAYMENT_AUTHORIZATION_HEADER, None)
        headers[selected_header] = build_payment_authorization(credential)

    def _require_secure_transport(self, url: httpx.URL) -> None:
        if url.scheme == "https":
            return
        if (
            self._allow_insecure_localhost
            and url.scheme == "http"
            and (url.host or "").lower() in {"localhost", "127.0.0.1", "::1"}
        ):
            return
        raise ValueError("MPP credentials and challenges require HTTPS")

    @staticmethod
    def _clone_request(
        request: httpx.Request,
        *,
        headers: httpx.Headers | dict[str, str] | None = None,
        extensions: dict[str, object] | None = None,
    ) -> httpx.Request:
        cloned_extensions = dict(request.extensions)
        if extensions:
            cloned_extensions.update(extensions)
        return httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers or request.headers,
            content=request.content,
            extensions=cloned_extensions,
        )

    @staticmethod
    def _resource_key(url: str) -> str:
        parts = urlsplit(url)
        resource = f"{parts.scheme}://{parts.netloc}{parts.path}"
        return f"{resource}?{parts.query}" if parts.query else resource

    @classmethod
    def _session_key(cls, request: httpx.Request) -> str:
        return f"{request.method.upper()} {cls._resource_key(str(request.url))}"

    @classmethod
    def _session_key_from_url(cls, url: str, *, method: str) -> str:
        return f"{method.upper()} {cls._resource_key(url)}"


def wrap_httpx_with_mpp_payment(
    signer: XRPLPaymentSigner,
    *,
    network: XRPLNetwork | None = None,
    currency: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
    payment_policy: XRPLPaymentPolicy | None = None,
    allow_insecure_localhost: bool = False,
) -> httpx.AsyncClient:
    payment_transport = XRPLPaymentTransport(
        signer,
        network=network,
        currency=currency,
        base_transport=transport,
        payment_policy=payment_policy,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    return httpx.AsyncClient(
        base_url=base_url or "",
        timeout=timeout,
        transport=payment_transport,
    )
