from __future__ import annotations

from email.utils import parsedate_to_datetime
import hashlib
from urllib.parse import urlsplit

import httpx

from xrpl_mpp_core import (
    FacilitatorSupportedResponse,
    MPPProblemDetails,
    PaymentCredential,
    PaymentReceipt,
    challenge_invoice_id,
    classic_address_from_did,
    decode_charge_payload,
    decode_challenge_request,
    decode_session_payload,
)
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    FacilitatorSettlementPendingError,
    FacilitatorTransportError,
)


SETTLEMENT_PENDING_PROBLEM_TYPE = "https://paymentauth.org/problems/settlement-pending"
_TRANSACTION_HASH_PREFIX = bytes.fromhex("54584E00")


def _signed_transaction_hash(blob: str) -> str:
    return hashlib.sha512(_TRANSACTION_HASH_PREFIX + bytes.fromhex(blob)).hexdigest()[:64].upper()


def _credential_payment_reference(credential: PaymentCredential) -> str | None:
    """Return a safe local reconciliation key without trusting the facilitator."""

    if credential.challenge.method != "xrpl":
        return None
    try:
        if credential.challenge.intent == "charge":
            payload = decode_charge_payload(credential)
            if payload.type == "hash":
                return payload.hash.upper()
            return _signed_transaction_hash(payload.blob)
        if credential.challenge.intent == "session":
            payload = decode_session_payload(credential)
            if payload.action == "open":
                return _signed_transaction_hash(payload.transaction)
            return f"{payload.channel_id.upper()}:{payload.amount}"
    except (TypeError, ValueError):
        return None
    return None


def _safe_retry_after(response: httpx.Response) -> str | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    value = raw.strip()
    if not value or len(value) > 128 or not value.isascii():
        return None
    if value.isdigit():
        return str(int(value))
    try:
        parseddate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def _safe_reference(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or not normalized.isascii()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        return None
    return normalized


def _references_match(left: str, right: str) -> bool:
    if len(left) == 64 and len(right) == 64:
        return left.upper() == right.upper()
    return left == right


def _fail_receipt(detail: str) -> None:
    raise FacilitatorProtocolError(f"Facilitator receipt is not credential-bound: {detail}")


def _validate_receipt_binding(
    credential: PaymentCredential,
    receipt: PaymentReceipt,
) -> None:
    """Reject a model-valid receipt that does not attest the submitted proof."""

    challenge = credential.challenge
    if receipt.method != challenge.method:
        _fail_receipt("method does not match the challenge")
    if receipt.challenge_id is not None and receipt.challenge_id != challenge.id:
        _fail_receipt("challengeId does not match the challenge")

    extra = receipt.model_extra or {}
    if extra.get("intent") not in {None, challenge.intent}:
        _fail_receipt("intent does not match the challenge")
    if extra.get("externalId") not in {None, challenge.id}:
        _fail_receipt("externalId does not match the challenge ID")

    try:
        terms = decode_challenge_request(challenge)
    except (TypeError, ValueError) as exc:
        raise FacilitatorProtocolError(
            "Submitted credential contains an invalid challenge request"
        ) from exc

    expected_network = (
        terms.method_details.network
        if terms.method_details is not None
        else None
    )
    if (
        expected_network is not None
        and receipt.network is not None
        and receipt.network != expected_network
    ):
        _fail_receipt("network does not match the challenge")
    if receipt.recipient is not None and receipt.recipient != terms.recipient:
        _fail_receipt("recipient does not match the challenge")
    if receipt.payer is not None and credential.source is not None:
        try:
            expected_payer = classic_address_from_did(
                credential.source,
                expected_network=expected_network,
            )
        except ValueError as exc:
            raise FacilitatorProtocolError(
                "Submitted credential contains an invalid XRPL source DID"
            ) from exc
        if receipt.payer != expected_payer:
            _fail_receipt("payer does not match the credential source")

    if challenge.intent == "charge":
        payload = decode_charge_payload(credential)
        expected_reference = (
            payload.hash.upper()
            if payload.type == "hash"
            else _signed_transaction_hash(payload.blob)
        )
        if not _references_match(receipt.reference, expected_reference):
            _fail_receipt("reference does not match the signed charge credential")
        if receipt.tx_hash is not None and not _references_match(
            receipt.tx_hash,
            expected_reference,
        ):
            _fail_receipt("txHash does not match the signed charge credential")
        details = terms.method_details
        expected_invoice_id = (
            details.invoice_id
            if details is not None and details.invoice_id is not None
            else challenge_invoice_id(challenge.id)
        )
        if (
            receipt.invoice_id is not None
            and receipt.invoice_id.upper() != expected_invoice_id.upper()
        ):
            _fail_receipt("invoiceId does not match the challenge")
        if any(
            value is not None
            for value in (receipt.action, receipt.channel_id, receipt.cumulative)
        ):
            _fail_receipt("PayChannel fields are not valid for a charge")
        return

    if challenge.intent != "session":
        _fail_receipt("unsupported challenge intent")

    payload = decode_session_payload(credential)
    if receipt.action is not None and receipt.action != payload.action:
        _fail_receipt("action does not match the session credential")
    if receipt.cumulative is not None and receipt.cumulative != payload.amount:
        _fail_receipt("cumulative does not match the session credential")

    if payload.action == "open":
        tx_hash = _signed_transaction_hash(payload.transaction)
        parts = receipt.reference.split(":")
        if (
            len(parts) != 3
            or parts[0] != "open"
            or len(parts[1]) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in parts[1])
            or not _references_match(parts[2], tx_hash)
        ):
            _fail_receipt("reference does not match the signed channel-open transaction")
        channel_id = parts[1].upper()
        if receipt.tx_hash is not None and not _references_match(receipt.tx_hash, tx_hash):
            _fail_receipt("txHash does not match the signed channel-open transaction")
    else:
        channel_id = payload.channel_id.upper()
        expected_reference = f"{channel_id}:{payload.amount}"
        if receipt.reference.upper() != expected_reference:
            _fail_receipt("reference does not match the signed session credential")

    if receipt.channel_id is not None and receipt.channel_id.upper() != channel_id:
        _fail_receipt("channelId does not match the signed session credential")


class XRPLFacilitatorClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout: float = 10.0,
        async_client: httpx.AsyncClient | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")
        parsed = urlsplit(normalized_base_url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Facilitator base_url must be an absolute origin URL")
        if parsed.scheme != "https" and not (
            allow_insecure_http and parsed.scheme == "http"
        ):
            raise ValueError(
                "Facilitator base_url must use HTTPS; insecure HTTP requires an explicit development opt-in"
            )
        self._base_url = normalized_base_url
        self._bearer_token = bearer_token
        self._timeout = timeout
        self._async_client = async_client
        self._owns_client = async_client is None
        self._supported_cache: FacilitatorSupportedResponse | None = None

    async def startup(self) -> None:
        await self.get_supported(force_refresh=False)

    async def aclose(self) -> None:
        if self._async_client is not None and self._owns_client:
            await self._async_client.aclose()

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupportedResponse:
        if self._supported_cache is not None and not force_refresh:
            return self._supported_cache

        response = await self._request("GET", "/supported")
        supported = FacilitatorSupportedResponse.model_validate(response)
        self._supported_cache = supported
        return supported

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        response = await self._request(
            "POST",
            "/charge",
            json={"credential": credential.model_dump(by_alias=True, exclude_none=True)},
            authenticated=True,
            stage="charge",
            payment_reference=_credential_payment_reference(credential),
            challenge_id=credential.challenge.id,
        )
        return self._parse_bound_receipt(credential, response)

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        response = await self._request(
            "POST",
            "/session",
            json={"credential": credential.model_dump(by_alias=True, exclude_none=True)},
            authenticated=True,
            stage="session",
            payment_reference=_credential_payment_reference(credential),
            challenge_id=credential.challenge.id,
        )
        return self._parse_bound_receipt(credential, response)

    @staticmethod
    def _parse_bound_receipt(
        credential: PaymentCredential,
        response: dict[str, object],
    ) -> PaymentReceipt:
        try:
            receipt = PaymentReceipt.model_validate(response)
        except (TypeError, ValueError) as exc:
            raise FacilitatorProtocolError(
                "Facilitator returned an invalid payment receipt"
            ) from exc
        try:
            _validate_receipt_binding(credential, receipt)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, FacilitatorProtocolError):
                raise
            raise FacilitatorProtocolError(
                "Facilitator receipt is not bound to the submitted credential"
            ) from exc
        return receipt

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        authenticated: bool = False,
        stage: str = "request",
        payment_reference: str | None = None,
        challenge_id: str | None = None,
    ) -> dict[str, object]:
        client = self._async_client
        if client is None:
            client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
            self._async_client = client

        headers = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        try:
            response = await client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json,
            )
        except httpx.TimeoutException as exc:
            raise FacilitatorTransportError(
                "Facilitator request timed out; payment outcome is unknown. "
                "Do not initiate another payment until paymentReference is reconciled.",
                payment_reference=payment_reference,
            ) from exc
        except httpx.HTTPError as exc:
            raise FacilitatorTransportError(
                "Facilitator connection failed; payment outcome is unknown. "
                "Do not initiate another payment until paymentReference is reconciled.",
                payment_reference=payment_reference,
            ) from exc

        pending = self._settlement_pending_error(
            response,
            local_reference=payment_reference,
            challenge_id=challenge_id,
        )
        if pending is not None:
            raise pending
        if response.status_code >= 500:
            raise FacilitatorTransportError(
                "Facilitator is unavailable; payment outcome is unknown. "
                "Do not initiate another payment until paymentReference is reconciled.",
                payment_reference=payment_reference,
            )

        if response.status_code == 401:
            raise FacilitatorProtocolError(
                f"Facilitator authentication failed: {self._extract_detail(response)}"
            )

        if response.status_code == 402:
            raise FacilitatorPaymentError(stage, response.status_code, self._extract_detail(response))

        if response.status_code >= 400:
            raise FacilitatorProtocolError(
                f"Facilitator returned unexpected status {response.status_code}: "
                f"{self._extract_detail(response)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FacilitatorProtocolError("Facilitator returned invalid JSON") from exc

        if not isinstance(body, dict):
            raise FacilitatorProtocolError("Facilitator returned a non-object JSON response")
        return body

    @staticmethod
    def _settlement_pending_error(
        response: httpx.Response,
        *,
        local_reference: str | None,
        challenge_id: str | None,
    ) -> FacilitatorSettlementPendingError | None:
        if response.status_code != 503:
            return None
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/problem+json":
            return None
        try:
            raw = response.json()
            problem = MPPProblemDetails.model_validate(raw)
        except (ValueError, TypeError):
            return None
        if problem.type != SETTLEMENT_PENDING_PROBLEM_TYPE or problem.status != 503:
            return None

        upstream_reference = _safe_reference(problem.payment_reference)
        trusted_local_reference = _safe_reference(local_reference)
        if upstream_reference is None:
            return None
        if (
            trusted_local_reference is not None
            and not _references_match(upstream_reference, trusted_local_reference)
        ):
            return None
        if (
            challenge_id is not None
            and problem.challenge_id is not None
            and problem.challenge_id != challenge_id
        ):
            return None

        title = problem.title.strip()[:200] or "Payment settlement pending"
        upstream_detail = problem.detail.strip() or "Payment settlement is pending."
        detail = (
            f"{upstream_detail} Do not initiate a fresh payment; reconcile "
            "paymentReference against validated ledger state first."
        )[:2048]
        sanitized = MPPProblemDetails(
            type=SETTLEMENT_PENDING_PROBLEM_TYPE,
            title=title,
            status=503,
            detail=detail,
            challengeId=challenge_id or problem.challenge_id,
            paymentReference=trusted_local_reference or upstream_reference,
        )
        return FacilitatorSettlementPendingError(
            sanitized,
            retry_after=_safe_retry_after(response),
        )

    @staticmethod
    def _extract_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text.strip() or "unknown facilitator error"

        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        return response.text.strip() or "unknown facilitator error"
