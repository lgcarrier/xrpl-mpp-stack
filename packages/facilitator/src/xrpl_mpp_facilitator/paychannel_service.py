from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from math import ceil
from typing import Annotated, Any, Literal, Protocol, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError
from xrpl.core import binarycodec
from xrpl.core.keypairs import derive_classic_address, is_valid_message

from xrpl_mpp_core.did import parse_xrpl_did
from xrpl_mpp_core.helpers import (
    challenge_is_expired,
    decode_challenge_request,
    verify_challenge_binding,
)
from xrpl_mpp_core.models import PaymentCredential
from xrpl_mpp_core.paychannel import (
    ChannelId,
    Drops,
    XRPLChannelClosePayload,
    XRPLChannelOpenPayload,
    XRPLChannelVoucherPayload,
    XRPLSessionRequest,
    validate_session_payload,
)
from xrpl_mpp_core.xrpl import ClassicAddress, XRPLModel, XRPLNetwork
from xrpl_mpp_facilitator.paychannel_store import (
    PayChannelAdvanceResult,
    PayChannelRecord,
    PayChannelStore,
    PayChannelStoreError,
)
from xrpl_mpp_facilitator.replay_store import ChallengeReplayStore
from xrpl_mpp_facilitator.settlement import SettlementPendingError


HEX_64_PATTERN = r"^[0-9A-Fa-f]{64}$"
MAX_UINT64 = (1 << 64) - 1
CHALLENGE_RETENTION_SKEW_SECONDS = 60
DateClock: TypeAlias = Callable[[], datetime]
TransactionDecoder: TypeAlias = Callable[[str], Mapping[str, Any]]
TxHash: TypeAlias = Annotated[str, Field(pattern=HEX_64_PATTERN)]
_NETWORK_ADAPTER = TypeAdapter(XRPLNetwork)
_ADDRESS_ADAPTER = TypeAdapter(ClassicAddress)
_DROPS_ADAPTER = TypeAdapter(Drops)
_TX_HASH_ADAPTER = TypeAdapter(TxHash)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decode_transaction(blob: str) -> Mapping[str, Any]:
    decoded = binarycodec.decode(blob)
    if not isinstance(decoded, dict):
        raise ValueError("decoded transaction is not an object")
    return decoded


class OpenChannelSubmission(XRPLModel):
    """Validated result returned after submitting a PaymentChannelCreate."""

    channel_id: ChannelId = Field(alias="channelId")
    tx_hash: str = Field(alias="txHash", pattern=HEX_64_PATTERN)


class PayChannelVerificationResult(XRPLModel):
    """Small verified result from which a normal MPP receipt can be built."""

    action: Literal["open", "voucher", "close"]
    challenge_id: str = Field(alias="challengeId")
    network: XRPLNetwork
    payer: ClassicAddress
    recipient: ClassicAddress
    channel_id: ChannelId = Field(alias="channelId")
    cumulative: Drops
    previous: Drops
    reference: str
    replayed: bool = False
    finalized: bool = False
    tx_hash: str | None = Field(default=None, alias="txHash", pattern=HEX_64_PATTERN)


class PayChannelVerificationError(ValueError):
    """Typed, fail-closed verification failure for facilitator error mapping."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


class OpenChannelSubmitter(Protocol):
    async def __call__(
        self,
        *,
        transaction_blob: str,
        transaction: Mapping[str, Any],
    ) -> OpenChannelSubmission:
        ...


class ChannelLedgerVerifier(Protocol):
    async def __call__(
        self,
        *,
        record: PayChannelRecord,
        cumulative: str,
    ) -> str | None:
        ...


class ChannelRecordLoader(Protocol):
    async def __call__(self, *, channel_id: str) -> PayChannelRecord:
        ...


class CloseChannelSettler(Protocol):
    async def __call__(self, *, record: PayChannelRecord) -> str:
        ...


class SignerAuthorizer(Protocol):
    async def __call__(self, *, account: str, signing_address: str) -> bool:
        ...


class CurrentLedgerIndex(Protocol):
    async def __call__(self) -> int:
        ...


class PayChannelService:
    """Verify XRPL MPP 0.2 session credentials without implicit network I/O.

    The store is the durable authority for cumulative accounting. Network work
    is explicit: opening requires ``open_submitter`` to return a validated
    channel ID, while optional ledger and close hooks can enforce current
    channel health and redeem the final claim in production. Unit tests can
    inject deterministic hooks and never contact rippled.
    """

    def __init__(
        self,
        *,
        store: PayChannelStore,
        replay_store: ChallengeReplayStore,
        challenge_secrets: Sequence[str],
        network: XRPLNetwork,
        recipient: str,
        payer_public_key: str,
        open_submitter: OpenChannelSubmitter | None = None,
        ledger_verifier: ChannelLedgerVerifier | None = None,
        channel_loader: ChannelRecordLoader | None = None,
        close_settler: CloseChannelSettler | None = None,
        signer_authorizer: SignerAuthorizer | None = None,
        current_ledger_index: CurrentLedgerIndex | None = None,
        minimum_settle_delay: int = 3_600,
        settlement_margin_seconds: int = 60,
        redemption_lease_seconds: int = 75,
        transaction_decoder: TransactionDecoder = _decode_transaction,
        now: DateClock = _utc_now,
    ) -> None:
        secret_values = (
            (challenge_secrets,)
            if isinstance(challenge_secrets, str)
            else challenge_secrets
        )
        normalized_secrets = tuple(secret.strip() for secret in secret_values if secret.strip())
        if not normalized_secrets:
            raise ValueError("at least one challenge-binding secret is required")

        self._store = store
        self._replay_store = replay_store
        self._challenge_secrets = normalized_secrets
        self._network = _NETWORK_ADAPTER.validate_python(network)
        self._recipient = _ADDRESS_ADAPTER.validate_python(recipient)
        self._public_key = payer_public_key.strip().upper()
        try:
            self._payer = _ADDRESS_ADAPTER.validate_python(
                derive_classic_address(self._public_key)
            )
        except Exception as exc:
            raise ValueError("payer_public_key is not a valid XRPL public key") from exc
        if self._payer == self._recipient:
            raise ValueError("PayChannel payer and recipient must be different accounts")
        if (
            isinstance(minimum_settle_delay, bool)
            or not isinstance(minimum_settle_delay, int)
            or minimum_settle_delay < 0
        ):
            raise ValueError("minimum_settle_delay must be a non-negative integer")
        if (
            isinstance(settlement_margin_seconds, bool)
            or not isinstance(settlement_margin_seconds, int)
            or settlement_margin_seconds < 0
        ):
            raise ValueError("settlement_margin_seconds must be a non-negative integer")
        if (
            isinstance(redemption_lease_seconds, bool)
            or not isinstance(redemption_lease_seconds, int)
            or redemption_lease_seconds <= 0
        ):
            raise ValueError("redemption_lease_seconds must be a positive integer")
        self._open_submitter = open_submitter
        self._ledger_verifier = ledger_verifier
        self._channel_loader = channel_loader
        self._close_settler = close_settler
        self._signer_authorizer = signer_authorizer
        self._current_ledger_index = current_ledger_index
        self._minimum_settle_delay = minimum_settle_delay
        self._settlement_margin_seconds = settlement_margin_seconds
        self._redemption_lease_seconds = redemption_lease_seconds
        self._transaction_decoder = transaction_decoder
        self._now = now

    @property
    def payer(self) -> str:
        return self._payer

    @property
    def recipient(self) -> str:
        return self._recipient

    @property
    def network(self) -> XRPLNetwork:
        return self._network

    def _timestamp_ms(self) -> int:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("service clock must return a timezone-aware datetime")
        return int(current.timestamp() * 1_000)

    def _challenge_retention_seconds(self, expires: str | None) -> int | None:
        if expires is None:
            # Without an authenticated upper bound on presentation, any finite
            # retention would eventually reopen the challenge replay window.
            return None
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        remaining = max(0, ceil((expires_at - self._now()).total_seconds()))
        return max(1, remaining + CHALLENGE_RETENTION_SKEW_SECONDS)

    async def _claim_challenge(self, credential: PaymentCredential) -> None:
        challenge = credential.challenge
        try:
            claimed = await self._replay_store.claim_challenge(
                f"{self._network}\x00{challenge.id}",
                retention_seconds=self._challenge_retention_seconds(challenge.expires),
            )
        except Exception as exc:
            raise PayChannelVerificationError(
                "CHALLENGE_REPLAY_CHECK_FAILED",
                "Could not atomically claim the PayChannel challenge",
            ) from exc
        if not claimed:
            raise PayChannelVerificationError(
                "CHANNEL_REPLAY",
                f"PayChannel challenge {challenge.id} was already used",
            )

    @staticmethod
    def _require_uint64(value: str, *, name: str) -> None:
        if int(value) > MAX_UINT64:
            raise PayChannelVerificationError(
                "INVALID_AMOUNT",
                f"{name} exceeds XRPL UInt64 range",
            )

    def _verify_envelope(
        self,
        credential: PaymentCredential,
    ) -> tuple[
        XRPLSessionRequest,
        XRPLChannelOpenPayload | XRPLChannelVoucherPayload | XRPLChannelClosePayload,
    ]:
        challenge = credential.challenge
        if challenge.method != "xrpl":
            raise PayChannelVerificationError(
                "UNSUPPORTED_METHOD",
                "PayChannel credentials require method xrpl",
            )
        if challenge.intent != "session":
            raise PayChannelVerificationError(
                "UNSUPPORTED_INTENT",
                "PayChannel credentials require intent session",
            )
        if not verify_challenge_binding(
            challenge,
            secrets=self._challenge_secrets,
        ):
            raise PayChannelVerificationError(
                "INVALID_CHALLENGE_BINDING",
                "Session challenge binding is invalid",
            )

        now = self._now()
        if now.tzinfo is None:
            raise ValueError("service clock must return a timezone-aware datetime")
        if challenge_is_expired(challenge, now=now):
            raise PayChannelVerificationError(
                "CHALLENGE_EXPIRED",
                "Session challenge has expired",
            )

        try:
            request = decode_challenge_request(challenge)
        except (ValidationError, TypeError, ValueError) as exc:
            raise PayChannelVerificationError(
                "INVALID_SESSION_REQUEST",
                "Challenge request does not match the XRPL session schema",
            ) from exc
        if not isinstance(request, XRPLSessionRequest):
            raise PayChannelVerificationError(
                "INVALID_SESSION_REQUEST",
                "Challenge did not decode to an XRPL session request",
            )

        request_network = request.method_details.network if request.method_details else None
        if request_network is not None and request_network != self._network:
            raise PayChannelVerificationError(
                "NETWORK_MISMATCH",
                f"Challenge network {request_network} does not match {self._network}",
            )
        if request.recipient != self._recipient:
            raise PayChannelVerificationError(
                "RECIPIENT_MISMATCH",
                "Challenge recipient does not match the configured recipient",
            )

        try:
            source = parse_xrpl_did(
                credential.source or "",
                expected_network=self._network,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise PayChannelVerificationError(
                "INVALID_SOURCE",
                "Credential source is not a valid XRPL DID for this network",
            ) from exc
        if source.address != self._payer:
            raise PayChannelVerificationError(
                "SOURCE_MISMATCH",
                "Credential source does not match the configured channel public key",
            )

        try:
            payload = validate_session_payload(credential.payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise PayChannelVerificationError(
                "INVALID_SESSION_PAYLOAD",
                "Credential payload does not exactly match an XRPL session action",
            ) from exc

        self._require_uint64(request.amount, name="challenge amount")
        self._require_uint64(payload.amount, name="credential cumulative amount")
        if request.method_details and request.method_details.cumulative_amount is not None:
            self._require_uint64(
                request.method_details.cumulative_amount,
                name="challenge cumulativeAmount",
            )

        if isinstance(payload, XRPLChannelOpenPayload):
            if request.channel_id != "":
                raise PayChannelVerificationError(
                    "CHANNEL_MISMATCH",
                    "An open challenge must carry an empty channelId",
                )
            prior = request.method_details.cumulative_amount if request.method_details else None
            if prior not in (None, "0"):
                raise PayChannelVerificationError(
                    "CUMULATIVE_MISMATCH",
                    "An open challenge must start from cumulativeAmount 0",
                )
        else:
            if not request.channel_id:
                raise PayChannelVerificationError(
                    "CHANNEL_MISMATCH",
                    "Voucher and close challenges require a channelId",
                )
            if payload.channel_id.upper() != request.channel_id.upper():
                raise PayChannelVerificationError(
                    "CHANNEL_MISMATCH",
                    "Credential channelId does not match the bound challenge",
                )

        return request, payload

    @staticmethod
    def _verify_claim_signature(
        *,
        channel_id: str,
        cumulative: str,
        signature: str,
        public_key: str,
    ) -> None:
        try:
            message = bytes.fromhex(
                binarycodec.encode_for_signing_claim(
                    {"channel": channel_id, "amount": cumulative}
                )
            )
            valid = is_valid_message(
                message,
                bytes.fromhex(signature),
                public_key,
            )
        except Exception as exc:
            raise PayChannelVerificationError(
                "INVALID_SIGNATURE",
                "PayChannel claim signature is invalid",
            ) from exc
        if not valid:
            raise PayChannelVerificationError(
                "INVALID_SIGNATURE",
                "PayChannel claim signature is invalid",
            )

    async def _decode_and_verify_open_transaction(
        self,
        *,
        transaction_blob: str,
        recipient: str,
        challenge_expires: str | None,
    ) -> tuple[Mapping[str, Any], str]:
        try:
            transaction = self._transaction_decoder(transaction_blob)
        except Exception as exc:
            raise PayChannelVerificationError(
                "INVALID_OPEN_TRANSACTION",
                "Could not decode PaymentChannelCreate transaction",
            ) from exc
        if not isinstance(transaction, Mapping):
            raise PayChannelVerificationError(
                "INVALID_OPEN_TRANSACTION",
                "Decoded PaymentChannelCreate transaction is not an object",
            )
        if transaction.get("TransactionType") != "PaymentChannelCreate":
            raise PayChannelVerificationError(
                "INVALID_OPEN_TRANSACTION",
                "Open action requires TransactionType PaymentChannelCreate",
            )
        if transaction.get("Signers"):
            raise PayChannelVerificationError(
                "INVALID_OPEN_TRANSACTION",
                "Multisigned PaymentChannelCreate transactions are not supported",
            )

        account = transaction.get("Account")
        destination = transaction.get("Destination")
        public_key = transaction.get("PublicKey")
        if account != self._payer:
            raise PayChannelVerificationError(
                "SOURCE_MISMATCH",
                "PaymentChannelCreate Account does not match the credential source",
            )
        if destination != recipient or destination != self._recipient:
            raise PayChannelVerificationError(
                "RECIPIENT_MISMATCH",
                "PaymentChannelCreate Destination does not match the challenge",
            )
        if not isinstance(public_key, str) or public_key.upper() != self._public_key:
            raise PayChannelVerificationError(
                "PUBLIC_KEY_MISMATCH",
                "PaymentChannelCreate PublicKey does not match the configured claim key",
            )

        raw_funded = transaction.get("Amount")
        try:
            funded = _DROPS_ADAPTER.validate_python(raw_funded)
        except (ValidationError, TypeError, ValueError) as exc:
            raise PayChannelVerificationError(
                "INVALID_FUNDING",
                "PaymentChannelCreate Amount must be an XRP drops string",
            ) from exc
        if int(funded) <= 0:
            raise PayChannelVerificationError(
                "INVALID_FUNDING",
                "PaymentChannelCreate Amount must be greater than zero",
            )
        self._require_uint64(funded, name="PaymentChannelCreate Amount")

        settle_delay = transaction.get("SettleDelay")
        if (
            isinstance(settle_delay, bool)
            or not isinstance(settle_delay, int)
            or settle_delay < self._minimum_settle_delay
        ):
            raise PayChannelVerificationError(
                "SETTLE_DELAY_TOO_SHORT",
                "PaymentChannelCreate SettleDelay is below the configured minimum",
            )

        last_ledger_sequence = transaction.get("LastLedgerSequence")
        if (
            isinstance(last_ledger_sequence, bool)
            or not isinstance(last_ledger_sequence, int)
            or last_ledger_sequence <= 0
        ):
            raise PayChannelVerificationError(
                "OPEN_TRANSACTION_UNBOUNDED",
                "PaymentChannelCreate requires LastLedgerSequence",
            )
        if self._current_ledger_index is None:
            raise PayChannelVerificationError(
                "LEDGER_INDEX_UNAVAILABLE",
                "Could not verify PaymentChannelCreate ledger expiry",
            )
        try:
            current_ledger = await self._current_ledger_index()
            if isinstance(current_ledger, bool) or int(current_ledger) < 0:
                raise ValueError("invalid current ledger index")
        except Exception as exc:
            raise PayChannelVerificationError(
                "LEDGER_INDEX_UNAVAILABLE",
                "Could not verify PaymentChannelCreate ledger expiry",
            ) from exc
        if last_ledger_sequence <= int(current_ledger):
            raise PayChannelVerificationError(
                "OPEN_TRANSACTION_EXPIRED",
                "PaymentChannelCreate LastLedgerSequence has expired",
            )
        if challenge_expires:
            expires_at = datetime.fromisoformat(challenge_expires.replace("Z", "+00:00"))
            remaining_ms = max(0, int((expires_at - self._now()).total_seconds() * 1_000))
            expiry_cap = int(current_ledger) + ceil(remaining_ms / 4_000) + 4
            if last_ledger_sequence > expiry_cap:
                raise PayChannelVerificationError(
                    "OPEN_TRANSACTION_OUTLIVES_CHALLENGE",
                    "PaymentChannelCreate LastLedgerSequence exceeds challenge expiry",
                )

        cancel_after = transaction.get("CancelAfter")
        if cancel_after is not None:
            if (
                isinstance(cancel_after, bool)
                or not isinstance(cancel_after, int)
                or cancel_after <= 0
            ):
                raise PayChannelVerificationError(
                    "INVALID_OPEN_TRANSACTION",
                    "PaymentChannelCreate CancelAfter is invalid",
                )
            ripple_now = int(self._now().timestamp()) - 946_684_800
            if cancel_after <= ripple_now + self._settlement_margin_seconds:
                raise PayChannelVerificationError(
                    "CHANNEL_CLOSING",
                    "PaymentChannelCreate CancelAfter is inside the settlement margin",
                )

        signing_public_key = transaction.get("SigningPubKey")
        transaction_signature = transaction.get("TxnSignature")
        if not isinstance(signing_public_key, str) or not signing_public_key:
            raise PayChannelVerificationError(
                "INVALID_OPEN_SIGNATURE",
                "PaymentChannelCreate SigningPubKey is required",
            )
        if not isinstance(transaction_signature, str) or not transaction_signature:
            raise PayChannelVerificationError(
                "INVALID_OPEN_SIGNATURE",
                "PaymentChannelCreate TxnSignature is required",
            )

        transaction_for_signing = dict(transaction)
        transaction_for_signing.pop("TxnSignature", None)
        try:
            signing_address = derive_classic_address(signing_public_key)
            signing_message = bytes.fromhex(
                binarycodec.encode_for_signing(transaction_for_signing)
            )
            signature_valid = is_valid_message(
                signing_message,
                bytes.fromhex(transaction_signature),
                signing_public_key,
            )
        except Exception as exc:
            raise PayChannelVerificationError(
                "INVALID_OPEN_SIGNATURE",
                "PaymentChannelCreate transaction signature is invalid",
            ) from exc
        if not signature_valid:
            raise PayChannelVerificationError(
                "INVALID_OPEN_SIGNATURE",
                "PaymentChannelCreate transaction signature is invalid",
            )

        if signing_address != account:
            if self._signer_authorizer is None:
                raise PayChannelVerificationError(
                    "UNAUTHORIZED_SIGNER",
                    "A non-master PaymentChannelCreate signer requires authorization",
                )
            try:
                authorized = await self._signer_authorizer(
                    account=str(account),
                    signing_address=signing_address,
                )
            except Exception as exc:
                raise PayChannelVerificationError(
                    "SIGNER_AUTHORIZATION_FAILED",
                    "Could not verify PaymentChannelCreate signing authority",
                ) from exc
            if not authorized:
                raise PayChannelVerificationError(
                    "UNAUTHORIZED_SIGNER",
                    "PaymentChannelCreate signer is not authorized for Account",
                )

        return transaction, funded

    @staticmethod
    def _raise_advance_failure(result: PayChannelAdvanceResult) -> None:
        code = {
            "replay": "CHANNEL_REPLAY",
            "regressed": "CUMULATIVE_REGRESSION",
            "short": "SHORT_PAYMENT",
        }[result.status]
        raise PayChannelVerificationError(
            code,
            f"PayChannel cumulative transition was {result.status}; previous={result.previous}",
        )

    @staticmethod
    def _store_error(error: PayChannelStoreError) -> PayChannelVerificationError:
        return PayChannelVerificationError(error.code, error.detail)

    async def _release_redemption_lease(
        self,
        *,
        channel_id: str,
        lease_id: str,
    ) -> None:
        try:
            await self._store.release_redemption(
                network=self._network,
                channel_id=channel_id,
                lease_id=lease_id,
            )
        except PayChannelStoreError as exc:
            raise self._store_error(exc) from exc
        except Exception as exc:
            raise PayChannelVerificationError(
                "REDEMPTION_LEASE_FAILED",
                "Could not release PayChannel redemption coordination",
            ) from exc

    def _assert_record_binding(
        self,
        record: PayChannelRecord,
        *,
        channel_id: str,
        recipient: str,
    ) -> None:
        if (
            record.network != self._network
            or record.channel_id != channel_id.upper()
            or record.payer != self._payer
            or record.recipient != recipient
            or record.recipient != self._recipient
        ):
            raise PayChannelVerificationError(
                "CHANNEL_BINDING_MISMATCH",
                "Stored PayChannel parties or network do not match the credential",
            )

    async def _verify_open(
        self,
        *,
        credential: PaymentCredential,
        request: XRPLSessionRequest,
        payload: XRPLChannelOpenPayload,
    ) -> PayChannelVerificationResult:
        if int(payload.amount) < int(request.amount):
            raise PayChannelVerificationError(
                "SHORT_PAYMENT",
                "Initial cumulative amount does not cover the requested amount",
            )
        transaction, funded = await self._decode_and_verify_open_transaction(
            transaction_blob=payload.transaction,
            recipient=request.recipient,
            challenge_expires=credential.challenge.expires,
        )
        if int(payload.amount) > int(funded):
            raise PayChannelVerificationError(
                "CHANNEL_EXHAUSTED",
                "Initial cumulative amount exceeds PaymentChannelCreate funding",
            )
        if self._open_submitter is None:
            raise PayChannelVerificationError(
                "OPEN_SUBMISSION_REQUIRED",
                "Opening a PayChannel requires a validated submission hook",
            )

        try:
            submitted = await self._open_submitter(
                transaction_blob=payload.transaction,
                transaction=transaction,
            )
            submitted = OpenChannelSubmission.model_validate(submitted)
        except PayChannelVerificationError:
            raise
        except SettlementPendingError:
            raise
        except Exception as exc:
            raise PayChannelVerificationError(
                "OPEN_SUBMISSION_FAILED",
                "PaymentChannelCreate submission did not return a validated channel",
            ) from exc

        channel_id = submitted.channel_id.upper()
        if int(payload.amount) > 0:
            self._verify_claim_signature(
                channel_id=channel_id,
                cumulative=payload.amount,
                signature=payload.signature,
                public_key=self._public_key,
            )

        # Claim only after the open transaction has validated and any initial
        # claim signature is proven. A malformed credential cannot burn a
        # challenge; once proven, one atomic claim wins across all replicas.
        await self._claim_challenge(credential)

        created = False
        try:
            record = await self._store.get(
                network=self._network,
                channel_id=channel_id,
            )
            if record is None:
                timestamp = self._timestamp_ms()
                candidate = PayChannelRecord(
                    network=self._network,
                    channel_id=channel_id,
                    payer=self._payer,
                    recipient=self._recipient,
                    funded=funded,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                try:
                    create_result = await self._store.create(candidate)
                    record = create_result.record
                    created = create_result.created
                except PayChannelStoreError as exc:
                    if exc.code != "CHANNEL_CONFLICT":
                        raise
                    record = await self._store.get(
                        network=self._network,
                        channel_id=channel_id,
                    )
                    if record is None:
                        raise
        except PayChannelStoreError as exc:
            raise self._store_error(exc) from exc

        self._assert_record_binding(
            record,
            channel_id=channel_id,
            recipient=request.recipient,
        )
        if record.funded != funded:
            raise PayChannelVerificationError(
                "CHANNEL_CONFLICT",
                "Stored channel funding differs from PaymentChannelCreate",
            )

        previous = record.cumulative
        replayed = False
        if int(payload.amount) > 0:
            try:
                advance = await self._store.advance(
                    network=self._network,
                    channel_id=channel_id,
                    cumulative=payload.amount,
                    requested=request.amount,
                    signature=payload.signature,
                    timestamp=self._timestamp_ms(),
                )
            except PayChannelStoreError as exc:
                raise self._store_error(exc) from exc
            if advance.status == "advanced":
                record = advance.record
                previous = advance.previous
                replayed = False
            else:
                self._raise_advance_failure(advance)
        elif not created:
            raise PayChannelVerificationError(
                "CHANNEL_REPLAY",
                "PaymentChannelCreate credential was already processed",
            )

        return PayChannelVerificationResult(
            action="open",
            challengeId=credential.challenge.id,
            network=self._network,
            payer=self._payer,
            recipient=self._recipient,
            channelId=channel_id,
            cumulative=record.cumulative,
            previous=previous,
            reference=f"open:{channel_id}:{submitted.tx_hash.upper()}",
            replayed=replayed,
            finalized=record.finalized,
            txHash=submitted.tx_hash.upper(),
        )

    async def _verify_claim_action(
        self,
        *,
        credential: PaymentCredential,
        request: XRPLSessionRequest,
        payload: XRPLChannelVoucherPayload | XRPLChannelClosePayload,
    ) -> PayChannelVerificationResult:
        channel_id = payload.channel_id.upper()
        try:
            record = await self._store.get(
                network=self._network,
                channel_id=channel_id,
            )
        except PayChannelStoreError as exc:
            raise self._store_error(exc) from exc
        if record is None:
            if self._channel_loader is None:
                raise PayChannelVerificationError(
                    "CHANNEL_NOT_FOUND",
                    f"PayChannel {self._network}/{channel_id} is not registered",
                )
            try:
                candidate = await self._channel_loader(channel_id=channel_id)
                self._assert_record_binding(
                    candidate,
                    channel_id=channel_id,
                    recipient=request.recipient,
                )
                created = await self._store.create(candidate)
                record = created.record
            except PayChannelVerificationError:
                raise
            except PayChannelStoreError as exc:
                if exc.code != "CHANNEL_CONFLICT":
                    raise self._store_error(exc) from exc
                record = await self._store.get(
                    network=self._network,
                    channel_id=channel_id,
                )
                if record is None:
                    raise self._store_error(exc) from exc
            except Exception as exc:
                raise PayChannelVerificationError(
                    "CHANNEL_LOOKUP_FAILED",
                    "Could not import the PayChannel from the validated ledger",
                ) from exc
        self._assert_record_binding(
            record,
            channel_id=channel_id,
            recipient=request.recipient,
        )

        action: Literal["voucher", "close"] = payload.action
        if record.finalized:
            raise PayChannelVerificationError(
                "CHANNEL_FINALIZED",
                f"PayChannel {self._network}/{channel_id} is finalized",
            )

        self._verify_claim_signature(
            channel_id=channel_id,
            cumulative=payload.amount,
            signature=payload.signature,
            public_key=self._public_key,
        )

        verified_funding: str | None = None
        if self._ledger_verifier is not None:
            try:
                verified_funding = await self._ledger_verifier(
                    record=record,
                    cumulative=payload.amount,
                )
            except PayChannelVerificationError:
                raise
            except Exception as exc:
                raise PayChannelVerificationError(
                    "CHANNEL_LEDGER_CHECK_FAILED",
                    "PayChannel ledger verification failed",
                ) from exc

        # Challenge IDs are single-use independently from the per-channel
        # cumulative high-water mark. Keep this after every proof and ledger
        # check, but before the durable high-water compare-and-set.
        await self._claim_challenge(credential)

        try:
            advance = await self._store.advance(
                network=self._network,
                channel_id=channel_id,
                cumulative=payload.amount,
                requested=request.amount,
                signature=payload.signature,
                funded=verified_funding,
                timestamp=self._timestamp_ms(),
            )
        except PayChannelStoreError as exc:
            raise self._store_error(exc) from exc

        if advance.status == "advanced":
            record = advance.record
            replayed = False
        elif (
            action == "close"
            and advance.status == "replay"
            and advance.exact_replay
            and not advance.record.finalized
        ):
            # A prior ledger settlement may have failed after the durable
            # high-water update. Permit the exact same final voucher to resume.
            record = advance.record
            replayed = True
        else:
            self._raise_advance_failure(advance)

        tx_hash: str | None = None
        if action == "close":
            if self._close_settler is not None:
                if int(record.redeemed) < int(record.cumulative):
                    try:
                        lease_id = await self._store.claim_redemption(
                            network=self._network,
                            channel_id=channel_id,
                            lease_seconds=self._redemption_lease_seconds,
                        )
                    except PayChannelStoreError as exc:
                        raise self._store_error(exc) from exc
                    except Exception as exc:
                        raise PayChannelVerificationError(
                            "REDEMPTION_LEASE_FAILED",
                            "Could not coordinate PayChannel claim redemption",
                        ) from exc
                    try:
                        latest = await self._store.get(
                            network=self._network,
                            channel_id=channel_id,
                        )
                    except PayChannelStoreError as exc:
                        raise self._store_error(exc) from exc
                    except Exception as exc:
                        raise PayChannelVerificationError(
                            "REDEMPTION_LEASE_FAILED",
                            "Could not reload coordinated PayChannel state",
                        ) from exc
                    if latest is None:
                        raise PayChannelVerificationError(
                            "CHANNEL_NOT_FOUND",
                            f"PayChannel {self._network}/{channel_id} is not registered",
                        )
                    self._assert_record_binding(
                        latest,
                        channel_id=channel_id,
                        recipient=request.recipient,
                    )
                    if (
                        latest.cumulative != record.cumulative
                        or latest.signature != record.signature
                    ):
                        if lease_id is not None:
                            await self._release_redemption_lease(
                                channel_id=channel_id,
                                lease_id=lease_id,
                            )
                        raise PayChannelVerificationError(
                            "CHANNEL_STATE_CHANGED",
                            "PayChannel high-water state changed during close settlement",
                        )
                    record = latest
                    if (
                        lease_id is None
                        and int(record.redeemed) < int(record.cumulative)
                    ):
                        # A background worker or another close request is
                        # settling this high-water mark. Do not submit a second
                        # on-ledger claim while its outcome is unresolved.
                        raise SettlementPendingError(
                            f"{channel_id}:{record.cumulative}"
                        )
                    if int(record.redeemed) < int(record.cumulative):
                        assert lease_id is not None
                        try:
                            raw_tx_hash = await self._close_settler(record=record)
                            tx_hash = _TX_HASH_ADAPTER.validate_python(
                                raw_tx_hash
                            ).upper()
                        except SettlementPendingError:
                            # Retain the lease until expiry because the signed
                            # claim may already have reached rippled.
                            raise
                        except PayChannelVerificationError:
                            raise
                        except Exception as exc:
                            raise PayChannelVerificationError(
                                "CLOSE_SETTLEMENT_FAILED",
                                "Could not redeem the final PayChannel claim",
                            ) from exc
                        try:
                            record = await self._store.mark_redeemed(
                                network=self._network,
                                channel_id=channel_id,
                                cumulative=record.cumulative,
                                reference=tx_hash.upper(),
                                timestamp=self._timestamp_ms(),
                            )
                        except PayChannelStoreError as exc:
                            raise self._store_error(exc) from exc
                    if lease_id is not None:
                        await self._release_redemption_lease(
                            channel_id=channel_id,
                            lease_id=lease_id,
                        )
                    if tx_hash is None and record.redemption_reference:
                        tx_hash = record.redemption_reference
                elif record.redemption_reference:
                    tx_hash = record.redemption_reference

            # A close proof is final for the MPP session even when no online
            # recipient signer is configured. If online redemption fails above,
            # this is deliberately not reached so a fresh challenge can retry
            # the exact stored final voucher.
            try:
                record = await self._store.finalize(
                    network=self._network,
                    channel_id=channel_id,
                    reason="closed",
                    expected_cumulative=payload.amount,
                    timestamp=self._timestamp_ms(),
                )
            except PayChannelStoreError as exc:
                raise self._store_error(exc) from exc

        return PayChannelVerificationResult(
            action=action,
            challengeId=credential.challenge.id,
            network=self._network,
            payer=self._payer,
            recipient=self._recipient,
            channelId=channel_id,
            cumulative=record.cumulative,
            previous=advance.previous,
            reference=f"{channel_id}:{record.cumulative}",
            replayed=replayed,
            finalized=record.finalized,
            txHash=tx_hash.upper() if tx_hash else None,
        )

    async def verify(
        self,
        credential: PaymentCredential,
    ) -> PayChannelVerificationResult:
        request, payload = self._verify_envelope(credential)
        if isinstance(payload, XRPLChannelOpenPayload):
            return await self._verify_open(
                credential=credential,
                request=request,
                payload=payload,
            )
        return await self._verify_claim_action(
            credential=credential,
            request=request,
            payload=payload,
        )
