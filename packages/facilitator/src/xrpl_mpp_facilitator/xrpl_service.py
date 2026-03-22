from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import secrets
from typing import Any, Literal

import structlog
from xrpl.clients import JsonRpcClient
from xrpl.core import binarycodec
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.models.requests import AccountInfo, Ledger, SubmitOnly, Tx
from xrpl.models.transactions import Payment

from xrpl_mpp_core import (
    TF_PARTIAL_PAYMENT,
    AssetKey,
    FacilitatorSupportedMethod,
    NormalizedAmount,
    PaymentCredential,
    PaymentReceipt,
    StructuredAmount,
    XRP_CODE,
    XRPLAsset,
    amount_from_structured_amount,
    canonical_asset_identifier,
    challenge_is_expired,
    decode_charge_payload,
    decode_challenge_request,
    decode_session_payload,
    format_amount,
    normalize_currency_code,
    parse_asset_identifier,
    supported_asset_keys,
    verify_challenge_binding,
)
from xrpl_mpp_facilitator.config import Settings, get_settings
from xrpl_mpp_facilitator.replay_store import ReplayReservation, ReplayStore, build_replay_store
from xrpl_mpp_facilitator.session_store import SessionState, SessionStore, build_session_store

logger = structlog.get_logger()
ACCOUNT_ROOT_FLAG_DISABLE_MASTER = 0x00100000
ACCEPTED_SUBMIT_ENGINE_RESULTS = frozenset({"tesSUCCESS", "terQUEUED"})


@dataclass(frozen=True)
class ValidatedPayment:
    signed_tx_blob: str
    tx: Payment
    invoice_id: str
    blob_hash: str
    amount: NormalizedAmount
    replay_reservation: ReplayReservation | None = None


class XRPLService:
    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        replay_store: ReplayStore | None = None,
        session_store: SessionStore | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.settings = app_settings or get_settings()
        self.client = JsonRpcClient(self.settings.XRPL_RPC_URL)
        self._supported_assets = supported_asset_keys(
            self.settings.NETWORK_ID,
            self.settings.ALLOWED_ISSUED_ASSETS,
        )
        self._allowed_issued_assets = {
            asset for asset in self._supported_assets if asset.issuer is not None
        }
        self._replay_store = replay_store or build_replay_store(
            self.settings,
            redis_client=redis_client,
        )
        self._session_store = session_store or build_session_store(
            self.settings,
            redis_client=redis_client,
        )

    async def _client_request(self, request: Any) -> Any:
        return await asyncio.to_thread(self.client.request, request)

    @staticmethod
    def _verify_single_signer_signature(tx_dict: dict[str, Any]) -> tuple[str, str]:
        if tx_dict.get("Signers"):
            raise ValueError("Multisigned transactions are not supported")

        account = tx_dict.get("Account")
        if not isinstance(account, str) or not account:
            raise ValueError("Transaction Account missing")

        signing_pub_key = tx_dict.get("SigningPubKey")
        txn_signature = tx_dict.get("TxnSignature")
        if (
            not isinstance(signing_pub_key, str)
            or not signing_pub_key
            or not isinstance(txn_signature, str)
            or not txn_signature
        ):
            raise ValueError("Transaction must be signed")

        try:
            signing_address = derive_classic_address(signing_pub_key)
        except Exception as exc:
            raise ValueError("SigningPubKey invalid") from exc

        tx_for_signing = dict(tx_dict)
        tx_for_signing.pop("TxnSignature", None)

        try:
            signing_payload = bytes.fromhex(binarycodec.encode_for_signing(tx_for_signing))
            signature_bytes = bytes.fromhex(txn_signature)
            signature_valid = is_valid_message(
                signing_payload,
                signature_bytes,
                signing_pub_key,
            )
        except Exception as exc:
            raise ValueError("Transaction signature invalid") from exc

        if not signature_valid:
            raise ValueError("Transaction signature invalid")

        return account, signing_address

    @staticmethod
    def _master_key_is_disabled(account_data: dict[str, Any]) -> bool:
        account_flags = account_data.get("account_flags")
        if isinstance(account_flags, dict):
            if bool(account_flags.get("disableMasterKey")) or bool(
                account_flags.get("DisableMasterKey")
            ):
                return True

        raw_flags = account_data.get("Flags")
        try:
            return bool(int(str(raw_flags), 0) & ACCOUNT_ROOT_FLAG_DISABLE_MASTER)
        except (TypeError, ValueError):
            return False

    async def _ensure_signing_address_authorized(
        self,
        *,
        account: str,
        signing_address: str,
    ) -> None:
        response = await self._client_request(
            AccountInfo(account=account, ledger_index="validated")
        )
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            raise ValueError("Unable to verify signing authority")

        account_data = result.get("account_data")
        if not isinstance(account_data, dict):
            raise ValueError("Unable to verify signing authority")

        authorized_addresses: set[str] = set()
        if not self._master_key_is_disabled(account_data):
            authorized_addresses.add(account)

        regular_key = account_data.get("RegularKey")
        if isinstance(regular_key, str):
            normalized_regular_key = regular_key.strip()
            if normalized_regular_key:
                authorized_addresses.add(normalized_regular_key)

        if signing_address not in authorized_addresses:
            raise ValueError("SigningPubKey is not authorized for Account")

    @staticmethod
    def _blob_hash(signed_tx_blob: str) -> str:
        return hashlib.sha256(signed_tx_blob.encode("utf-8")).hexdigest()

    async def _decode_payment(self, signed_tx_blob: str) -> Payment:
        tx_dict = binarycodec.decode(signed_tx_blob)
        if tx_dict.get("TransactionType") != "Payment":
            raise ValueError("TransactionType must be Payment")
        account, signing_address = self._verify_single_signer_signature(tx_dict)
        await self._ensure_signing_address_authorized(
            account=account,
            signing_address=signing_address,
        )
        payment = Payment.from_xrpl(tx_dict)
        if not payment.is_signed():
            raise ValueError("Transaction must be signed")
        return payment

    def _resolve_invoice_id(
        self,
        payment: Payment,
        blob_hash: str,
        provided_invoice_id: str | None,
    ) -> str:
        embedded_invoice_id = payment.invoice_id
        if embedded_invoice_id:
            if provided_invoice_id and provided_invoice_id != embedded_invoice_id:
                raise ValueError("Provided invoice_id does not match transaction InvoiceID")
            return embedded_invoice_id

        if provided_invoice_id:
            raise ValueError("Provided invoice_id requires transaction InvoiceID")

        return blob_hash[:32]

    @staticmethod
    def _normalize_issued_amount_fields(
        currency: Any,
        issuer: Any,
        raw_value: Any,
    ) -> NormalizedAmount:
        normalized_currency = normalize_currency_code(str(currency))
        if normalized_currency == XRP_CODE:
            raise ValueError("XRP amounts must be expressed in drops")

        normalized_issuer = str(issuer).strip()
        if not normalized_issuer:
            raise ValueError("Issued asset issuer missing")

        if raw_value is None:
            raise ValueError("Issued asset value missing")

        try:
            value = Decimal(str(raw_value))
        except InvalidOperation as exc:
            raise ValueError("Issued asset value invalid") from exc
        if value <= 0:
            raise ValueError("Issued asset amount must be greater than zero")

        return NormalizedAmount(
            asset=AssetKey(code=normalized_currency, issuer=normalized_issuer),
            value=value,
        )

    def _normalize_amount(self, amount: Any) -> NormalizedAmount:
        if isinstance(amount, int):
            drops = amount
            if drops < 0:
                raise ValueError("Negative XRP amount not allowed")
            return NormalizedAmount(
                asset=AssetKey(code=XRP_CODE, issuer=None),
                value=Decimal(drops),
                drops=drops,
            )

        if isinstance(amount, str):
            if amount == "unavailable":
                raise ValueError("Delivered amount unavailable")
            drops = int(amount)
            if drops < 0:
                raise ValueError("Negative XRP amount not allowed")
            return NormalizedAmount(
                asset=AssetKey(code=XRP_CODE, issuer=None),
                value=Decimal(drops),
                drops=drops,
            )

        if isinstance(amount, dict):
            return self._normalize_issued_amount_fields(
                currency=amount.get("currency", ""),
                issuer=amount.get("issuer", ""),
                raw_value=amount.get("value"),
            )

        if all(hasattr(amount, field) for field in ("currency", "issuer", "value")):
            return self._normalize_issued_amount_fields(
                currency=getattr(amount, "currency"),
                issuer=getattr(amount, "issuer"),
                raw_value=getattr(amount, "value"),
            )

        raise ValueError("Unsupported payment amount format")

    def _normalize_requested_amount(self, asset_identifier: str, amount: str) -> NormalizedAmount:
        asset = parse_asset_identifier(asset_identifier)
        if asset.issuer is None:
            try:
                drops = int(amount)
            except ValueError as exc:
                raise ValueError("XRP request amount must be an integer string") from exc
            return NormalizedAmount(
                asset=AssetKey(code=asset.code, issuer=None),
                value=Decimal(drops),
                drops=drops,
            )

        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise ValueError("Issued-asset request amount is invalid") from exc
        if value <= 0:
            raise ValueError("Issued-asset request amount must be greater than zero")
        return NormalizedAmount(
            asset=AssetKey(code=asset.code, issuer=asset.issuer),
            value=value,
        )

    def _ensure_policy(self, payment: Payment, amount: NormalizedAmount) -> None:
        raw_flags = getattr(payment, "flags", 0) or 0
        flags = int(raw_flags, 0) if isinstance(raw_flags, str) else int(raw_flags)
        if flags & TF_PARTIAL_PAYMENT:
            raise ValueError("Partial payments are not supported")

        if payment.destination != self.settings.MY_DESTINATION_ADDRESS:
            raise ValueError("Wrong destination address")

        if amount.asset.code == XRP_CODE:
            if amount.drops is None or amount.drops < self.settings.MIN_XRP_DROPS:
                raise ValueError("Payment below minimum amount")
            return

        if amount.asset not in self._allowed_issued_assets:
            raise ValueError(
                f"Unsupported issued asset: {amount.asset.code}:{amount.asset.issuer}"
            )

    @staticmethod
    def _to_asset_descriptor(asset: AssetKey) -> XRPLAsset:
        return XRPLAsset(code=asset.code, issuer=asset.issuer)

    @classmethod
    def _to_structured_amount(cls, amount: NormalizedAmount) -> StructuredAmount:
        return StructuredAmount(
            value=str(amount.drops if amount.drops is not None else amount.value),
            unit="drops" if amount.drops is not None else "issued",
            asset=cls._to_asset_descriptor(amount.asset),
            drops=amount.drops,
        )

    def supported_assets(self) -> list[XRPLAsset]:
        return [self._to_asset_descriptor(asset) for asset in self._supported_assets]

    def supported_methods(self) -> list[FacilitatorSupportedMethod]:
        return [
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=["charge", "session"],
                network=self.settings.NETWORK_ID,
                assets=self.supported_assets(),
                settlementMode=self.settings.SETTLEMENT_MODE,
            )
        ]

    async def _get_latest_validated_ledger_sequence(self) -> int:
        response = await self._client_request(Ledger(ledger_index="validated"))
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            raise ValueError("Unable to determine current validated ledger")

        ledger_index = result.get("ledger_index")
        try:
            return int(ledger_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unable to determine current validated ledger") from exc

    async def _ensure_payment_freshness(self, payment: Payment) -> None:
        if self.settings.GATEWAY_AUTH_MODE != "redis_gateways":
            return

        last_ledger_sequence = getattr(payment, "last_ledger_sequence", None)
        if last_ledger_sequence is None:
            raise ValueError("Transaction LastLedgerSequence required in redis_gateways mode")

        try:
            last_ledger_sequence_int = int(last_ledger_sequence)
        except (TypeError, ValueError) as exc:
            raise ValueError("Transaction LastLedgerSequence invalid") from exc

        current_validated_ledger = await self._get_latest_validated_ledger_sequence()
        if last_ledger_sequence_int <= current_validated_ledger:
            raise ValueError("Transaction LastLedgerSequence expired")

        max_allowed_ledger = current_validated_ledger + self.settings.MAX_PAYMENT_LEDGER_WINDOW
        if last_ledger_sequence_int > max_allowed_ledger:
            raise ValueError("Transaction LastLedgerSequence too far in the future")

    async def _validate_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None,
        replay_mode: Literal["verify", "settle"],
        replay_scope: Literal["invoice_and_blob", "blob_only"] = "invoice_and_blob",
    ) -> ValidatedPayment:
        tx = await self._decode_payment(signed_tx_blob)
        blob_hash = self._blob_hash(signed_tx_blob)
        invoice_id = self._resolve_invoice_id(tx, blob_hash, provided_invoice_id)
        amount = self._normalize_amount(tx.amount)
        self._ensure_policy(tx, amount)
        await self._ensure_payment_freshness(tx)
        replay_invoice_id = invoice_id if replay_scope == "invoice_and_blob" else None
        replay_reservation: ReplayReservation | None = None
        if replay_mode == "settle":
            replay_reservation = await self._replay_store.reserve(replay_invoice_id, blob_hash)
        else:
            await self._replay_store.guard_available(replay_invoice_id, blob_hash)
        return ValidatedPayment(
            signed_tx_blob=signed_tx_blob,
            tx=tx,
            invoice_id=invoice_id,
            blob_hash=blob_hash,
            amount=amount,
            replay_reservation=replay_reservation,
        )

    def _extract_delivered_amount(self, result: dict[str, Any]) -> NormalizedAmount:
        meta = result.get("meta") or {}
        delivered_amount = meta.get("delivered_amount")
        if delivered_amount is None:
            delivered_amount = meta.get("DeliveredAmount")
        if delivered_amount is None:
            raise ValueError("Validated transaction missing delivered_amount")
        return self._normalize_amount(delivered_amount)

    @staticmethod
    def _ensure_delivered_amount_matches(
        expected: NormalizedAmount,
        delivered: NormalizedAmount,
    ) -> None:
        if expected.asset != delivered.asset:
            raise ValueError("Validated transaction delivered unexpected asset")

        if expected.drops is not None:
            if delivered.drops is None or expected.drops != delivered.drops:
                raise ValueError("Validated transaction delivered wrong XRP amount")
            return

        if delivered.drops is not None or expected.value != delivered.value:
            raise ValueError("Validated transaction delivered wrong issued-asset amount")

    @staticmethod
    def _submit_failure_detail(result: dict[str, Any], *fallbacks: Any) -> str:
        for candidate in (
            result.get("engine_result_message"),
            result.get("error_message"),
            result.get("error"),
            *fallbacks,
        ):
            if candidate is None:
                continue
            rendered = getattr(candidate, "value", candidate)
            if isinstance(rendered, str):
                rendered = rendered.strip()
                if rendered:
                    return rendered
                continue
            return str(rendered)
        return "unknown submission failure"

    @classmethod
    def _ensure_submit_succeeded(cls, response: Any) -> dict[str, Any]:
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            result = {}

        status = getattr(response, "status", None)
        status_value = getattr(status, "value", status)
        if status_value is not None and status_value != "success":
            detail = cls._submit_failure_detail(result, status_value)
            raise ValueError(f"XRPL submission failed: {detail}")

        engine_result = result.get("engine_result")
        if not isinstance(engine_result, str):
            detail = cls._submit_failure_detail(result)
            if detail == "unknown submission failure":
                raise ValueError("XRPL submission failed: missing engine_result")
            raise ValueError(f"XRPL submission failed: missing engine_result ({detail})")

        if engine_result not in ACCEPTED_SUBMIT_ENGINE_RESULTS:
            detail = cls._submit_failure_detail(result)
            if detail != engine_result:
                raise ValueError(f"XRPL submission rejected: {engine_result} ({detail})")
            raise ValueError(f"XRPL submission rejected: {engine_result}")

        return result

    async def verify_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None = None,
    ) -> StructuredAmount:
        validated_payment = await self._validate_payment(
            signed_tx_blob,
            provided_invoice_id,
            replay_mode="verify",
        )
        return self._to_structured_amount(validated_payment.amount)

    async def settle_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None = None,
    ) -> tuple[str, Literal["submitted", "validated"]]:
        validated_payment = await self._validate_payment(
            signed_tx_blob,
            provided_invoice_id,
            replay_mode="settle",
        )
        return await self._settle_validated_payment(validated_payment)

    async def _settle_validated_payment(
        self,
        validated_payment: ValidatedPayment,
    ) -> tuple[str, Literal["submitted", "validated"]]:
        response = await self._client_request(SubmitOnly(tx_blob=validated_payment.signed_tx_blob))
        return await self._finalize_submission(validated_payment, response)

    async def _release_replay_reservation(
        self,
        validated_payment: ValidatedPayment,
    ) -> None:
        if validated_payment.replay_reservation is not None:
            await self._replay_store.release_pending(validated_payment.replay_reservation)

    async def _finalize_submission(
        self,
        validated_payment: ValidatedPayment,
        response: Any,
    ) -> tuple[str, Literal["submitted", "validated"]]:
        try:
            submit_result = self._ensure_submit_succeeded(response)
            engine_result = str(submit_result.get("engine_result"))
            tx_hash = validated_payment.tx.get_hash()
            if engine_result == "terQUEUED":
                logger.info("payment_queued", tx_hash=tx_hash)

            if self.settings.SETTLEMENT_MODE == "validated":
                for _ in range(self.settings.VALIDATION_TIMEOUT):
                    tx_info = await self._client_request(Tx(transaction=tx_hash))
                    if tx_info.result.get("validated"):
                        reservation = validated_payment.replay_reservation
                        if reservation is None:
                            raise ValueError("Replay reservation missing for settlement")
                        await self._replay_store.mark_processed(reservation)
                        delivered_amount = self._extract_delivered_amount(tx_info.result)
                        self._ensure_delivered_amount_matches(
                            validated_payment.amount,
                            delivered_amount,
                        )
                        logger.info("payment_validated", tx_hash=tx_hash)
                        return tx_hash, "validated"
                    await asyncio.sleep(1)

                reservation = validated_payment.replay_reservation
                if reservation is not None:
                    await self._replay_store.release_pending(reservation)
                raise ValueError("Validation timeout exceeded")

            reservation = validated_payment.replay_reservation
            if reservation is None:
                raise ValueError("Replay reservation missing for settlement")
            await self._replay_store.mark_processed(reservation)
            logger.info("payment_submitted", tx_hash=tx_hash, engine_result=engine_result)
            return tx_hash, "submitted"
        except Exception:
            if validated_payment.replay_reservation is not None:
                await self._replay_store.release_pending(validated_payment.replay_reservation)
            raise

    def _assert_credential(self, credential: PaymentCredential, *, intent: str) -> None:
        if credential.challenge.method != "xrpl":
            raise ValueError("Unsupported payment method")
        if credential.challenge.intent != intent:
            raise ValueError(f"Credential intent must be {intent}")
        if not verify_challenge_binding(
            credential.challenge,
            secret=self.settings.MPP_CHALLENGE_SECRET.get_secret_value(),
        ):
            raise ValueError("Challenge binding invalid")
        if challenge_is_expired(credential.challenge):
            raise ValueError("Challenge expired")

    def _assert_payment_matches_request(
        self,
        *,
        validated_payment: ValidatedPayment,
        recipient: str,
        asset_identifier: str,
        amount: str,
        minimum_only: bool = False,
    ) -> None:
        requested = self._normalize_requested_amount(asset_identifier, amount)
        if validated_payment.tx.destination != recipient:
            raise ValueError("Payment recipient does not match the request")
        if requested.asset != validated_payment.amount.asset:
            raise ValueError("Payment asset does not match the request")
        if minimum_only:
            if validated_payment.amount.value < requested.value:
                raise ValueError("Payment amount is below the required minimum")
            return
        if requested.drops is not None:
            if validated_payment.amount.drops != requested.drops:
                raise ValueError("Payment amount does not match the request")
            return
        if validated_payment.amount.value != requested.value:
            raise ValueError("Payment amount does not match the request")

    def _build_receipt(
        self,
        *,
        reference: str,
        challenge_id: str,
        intent: Literal["charge", "session"],
        payer: str,
        recipient: str,
        network: str,
        asset: AssetKey,
        amount: NormalizedAmount | None,
        settlement_status: Literal["submitted", "validated", "session_open", "session_active", "session_closed"] | None,
        invoice_id: str | None = None,
        session_id: str | None = None,
        session_token: str | None = None,
        tx_hash: str | None = None,
        spent_total: str | None = None,
        available_balance: str | None = None,
        prepaid_total: str | None = None,
        last_action: Literal["open", "use", "top_up", "close"] | None = None,
    ) -> PaymentReceipt:
        structured_amount = self._to_structured_amount(amount) if amount is not None else None
        return PaymentReceipt(
            method="xrpl",
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            reference=reference,
            challengeId=challenge_id,
            intent=intent,
            network=network,
            payer=payer,
            recipient=recipient,
            invoiceId=invoice_id,
            sessionId=session_id,
            sessionToken=session_token,
            txHash=tx_hash,
            settlementStatus=settlement_status,
            asset=self._to_asset_descriptor(asset),
            amount=structured_amount,
            spentTotal=spent_total,
            availableBalance=available_balance,
            prepaidTotal=prepaid_total,
            lastAction=last_action,
        )

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        self._assert_credential(credential, intent="charge")
        challenge_request = decode_challenge_request(credential.challenge)
        payload = decode_charge_payload(credential)
        if not hasattr(challenge_request, "method_details"):
            raise ValueError("Charge request is malformed")

        validated_payment = await self._validate_payment(
            payload.signed_tx_blob,
            challenge_request.method_details.invoice_id,
            replay_mode="settle",
        )
        self._assert_payment_matches_request(
            validated_payment=validated_payment,
            recipient=challenge_request.recipient,
            asset_identifier=challenge_request.currency,
            amount=challenge_request.amount,
        )
        tx_hash, settlement_status = await self._settle_validated_payment(validated_payment)
        return self._build_receipt(
            reference=tx_hash,
            challenge_id=credential.challenge.id,
            intent="charge",
            payer=validated_payment.tx.account,
            recipient=validated_payment.tx.destination,
            network=challenge_request.method_details.network,
            asset=validated_payment.amount.asset,
            amount=validated_payment.amount,
            settlement_status=settlement_status,
            invoice_id=validated_payment.invoice_id,
            tx_hash=tx_hash,
        )

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        self._assert_credential(credential, intent="session")
        challenge_request = decode_challenge_request(credential.challenge)
        payload = decode_session_payload(credential)
        session_id = challenge_request.method_details.session_id
        route_amount = self._normalize_requested_amount(
            challenge_request.currency,
            challenge_request.amount,
        )
        session_binding = {
            "recipient": challenge_request.recipient,
            "asset_identifier": challenge_request.currency,
            "network": challenge_request.method_details.network,
            "unit_amount": challenge_request.method_details.unit_amount,
            "min_prepay_amount": challenge_request.method_details.min_prepay_amount,
        }

        if payload.action == "open":
            validated_payment = await self._validate_payment(
                payload.signed_tx_blob or "",
                session_id,
                replay_mode="settle",
            )
            self._assert_payment_matches_request(
                validated_payment=validated_payment,
                recipient=challenge_request.recipient,
                asset_identifier=challenge_request.currency,
                amount=challenge_request.method_details.min_prepay_amount,
                minimum_only=True,
            )
            session_token = secrets.token_urlsafe(32)
            try:
                await self._session_store.begin_open_session(
                    session_id=session_id,
                    session_token=session_token,
                    payer=validated_payment.tx.account,
                    recipient=validated_payment.tx.destination,
                    asset_identifier=challenge_request.currency,
                    network=challenge_request.method_details.network,
                    unit_amount=challenge_request.method_details.unit_amount,
                    min_prepay_amount=challenge_request.method_details.min_prepay_amount,
                    idle_timeout_seconds=challenge_request.method_details.idle_timeout_seconds
                    or self.settings.SESSION_IDLE_TIMEOUT_SECONDS,
                    prepaid_total=validated_payment.amount.value,
                    initial_spend=route_amount.value,
                    action_id=credential.challenge.id,
                )
            except Exception:
                await self._release_replay_reservation(validated_payment)
                raise
            try:
                tx_hash, settlement_status = await self._settle_validated_payment(validated_payment)
            except Exception:
                await self._session_store.abort_open_session(
                    session_id=session_id,
                    action_id=credential.challenge.id,
                )
                raise
            state = await self._session_store.commit_open_session(
                session_id=session_id,
                action_id=credential.challenge.id,
                initial_spend=route_amount.value,
            )
            return self._build_session_receipt(
                challenge_id=credential.challenge.id,
                state=state,
                request_amount=route_amount,
                action="open",
                session_token=session_token,
                tx_hash=tx_hash,
                settlement_status="session_open",
            )

        if payload.action == "use":
            state = await self._session_store.consume(
                session_id=session_id,
                session_token=payload.session_token or "",
                recipient=session_binding["recipient"],
                asset_identifier=session_binding["asset_identifier"],
                network=session_binding["network"],
                unit_amount=session_binding["unit_amount"],
                min_prepay_amount=session_binding["min_prepay_amount"],
                amount=route_amount.value,
                action_id=credential.challenge.id,
            )
            return self._build_session_receipt(
                challenge_id=credential.challenge.id,
                state=state,
                request_amount=route_amount,
                action="use",
                session_token=None,
                tx_hash=None,
                settlement_status="session_active",
            )

        if payload.action == "top_up":
            validated_payment = await self._validate_payment(
                payload.signed_tx_blob or "",
                session_id,
                replay_mode="settle",
                replay_scope="blob_only",
            )
            self._assert_payment_matches_request(
                validated_payment=validated_payment,
                recipient=challenge_request.recipient,
                asset_identifier=challenge_request.currency,
                amount=challenge_request.method_details.min_prepay_amount,
                minimum_only=True,
            )
            try:
                await self._session_store.begin_top_up(
                    session_id=session_id,
                    session_token=payload.session_token or "",
                    recipient=session_binding["recipient"],
                    asset_identifier=session_binding["asset_identifier"],
                    network=session_binding["network"],
                    unit_amount=session_binding["unit_amount"],
                    min_prepay_amount=session_binding["min_prepay_amount"],
                    amount=validated_payment.amount.value,
                    action_id=credential.challenge.id,
                )
            except Exception:
                await self._release_replay_reservation(validated_payment)
                raise
            try:
                tx_hash, _ = await self._settle_validated_payment(validated_payment)
            except Exception:
                await self._session_store.abort_top_up(
                    session_id=session_id,
                    session_token=payload.session_token or "",
                    action_id=credential.challenge.id,
                )
                raise
            state = await self._session_store.commit_top_up(
                session_id=session_id,
                session_token=payload.session_token or "",
                action_id=credential.challenge.id,
            )
            return self._build_session_receipt(
                challenge_id=credential.challenge.id,
                state=state,
                request_amount=validated_payment.amount,
                action="top_up",
                session_token=None,
                tx_hash=tx_hash,
                settlement_status="session_active",
            )

        if payload.action == "close":
            state = await self._session_store.close_session(
                session_id=session_id,
                session_token=payload.session_token or "",
                recipient=session_binding["recipient"],
                asset_identifier=session_binding["asset_identifier"],
                network=session_binding["network"],
                unit_amount=session_binding["unit_amount"],
                min_prepay_amount=session_binding["min_prepay_amount"],
                action_id=credential.challenge.id,
            )
            return self._build_session_receipt(
                challenge_id=credential.challenge.id,
                state=state,
                request_amount=None,
                action="close",
                session_token=None,
                tx_hash=None,
                settlement_status="session_closed",
            )

        raise ValueError(f"Unsupported session action {payload.action!r}")

    def _build_session_receipt(
        self,
        *,
        challenge_id: str,
        state: SessionState,
        request_amount: NormalizedAmount | None,
        action: Literal["open", "use", "top_up", "close"],
        session_token: str | None,
        tx_hash: str | None,
        settlement_status: Literal["session_open", "session_active", "session_closed"],
    ) -> PaymentReceipt:
        asset = parse_asset_identifier(state.asset_identifier)
        return self._build_receipt(
            reference=state.session_id,
            challenge_id=challenge_id,
            intent="session",
            payer=state.payer,
            recipient=state.recipient,
            network=state.network,
            asset=AssetKey(code=asset.code, issuer=asset.issuer),
            amount=request_amount,
            settlement_status=settlement_status,
            session_id=state.session_id,
            session_token=session_token,
            tx_hash=tx_hash,
            spent_total=state.spent_total,
            available_balance=state.available_balance,
            prepaid_total=state.prepaid_total,
            last_action=action,
        )
