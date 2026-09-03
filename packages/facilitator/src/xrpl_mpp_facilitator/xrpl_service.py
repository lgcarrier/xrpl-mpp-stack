from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import math
from typing import Any, Literal

import structlog
from xrpl.clients import JsonRpcClient
from xrpl.core import binarycodec
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.models.requests import AccountInfo, Ledger, LedgerEntry, SubmitOnly, Tx
from xrpl.models.transactions import Payment, PaymentChannelClaim, PaymentChannelCreate
from xrpl.transaction import autofill, submit_and_wait

from xrpl_mpp_core import (
    TF_PARTIAL_PAYMENT,
    AssetKey,
    FacilitatorSupportedMethod,
    IssuedCurrency,
    MPToken,
    NormalizedAmount,
    PaymentCredential,
    PaymentReceipt,
    XRP,
    XRP_CODE,
    challenge_invoice_id,
    challenge_is_expired,
    decode_charge_payload,
    decode_challenge_request,
    normalize_currency_code,
    parse_currency,
    parse_xrpl_did,
    serialize_currency,
    supported_asset_keys,
    verify_challenge_binding,
    xrpl_currency_code,
)
from xrpl_mpp_facilitator.config import Settings, get_settings
from xrpl_mpp_facilitator.paychannel_service import (
    OpenChannelSubmission,
    PayChannelService,
    PayChannelVerificationError,
)
from xrpl_mpp_facilitator.paychannel_store import PayChannelRecord, RedisPayChannelStore
from xrpl_mpp_facilitator.paychannel_worker import PayChannelRedemptionWorker
from xrpl_mpp_facilitator.redis_utils import create_async_redis_client
from xrpl_mpp_facilitator.recipient_signer import (
    LocalSeedRecipientSigner,
    RecipientSigner,
)
from xrpl_mpp_facilitator.replay_store import (
    ReplayReservation,
    ReplayStore,
    build_replay_store,
    replay_retention_seconds,
)
from xrpl_mpp_facilitator.settlement import SettlementPendingError

logger = structlog.get_logger()
ACCOUNT_ROOT_FLAG_DISABLE_MASTER = 0x00100000
ACCEPTED_SUBMIT_ENGINE_RESULTS = frozenset(
    {"tesSUCCESS", "terQUEUED", "tefALREADY", "tefPAST_SEQ"}
)


class SubmissionRejectedError(ValueError):
    """A rippled submission response that definitively did not accept the tx."""


@dataclass(frozen=True)
class ValidatedPayment:
    tx: Payment
    raw: dict[str, Any]
    invoice_id: str
    tx_hash: str
    amount: NormalizedAmount
    signed_tx_blob: str | None = None
    replay_reservation: ReplayReservation | None = None


class XRPLService:
    """Facilitator verifier for native MPP 0.2 XRPL credentials."""

    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        replay_store: ReplayStore | None = None,
        paychannel_service: PayChannelService | None = None,
        redis_client: Any | None = None,
        client: JsonRpcClient | None = None,
        recipient_signer: RecipientSigner | None = None,
    ) -> None:
        self.settings = app_settings or get_settings()
        self.client = client or JsonRpcClient(self.settings.XRPL_RPC_URL)
        self._supported_assets = supported_asset_keys(
            self.settings.NETWORK_ID,
            self.settings.ALLOWED_ISSUED_ASSETS,
        )
        self._allowed_issued_assets = {
            asset for asset in self._supported_assets if asset.issuer is not None
        }
        self._allowed_mpt_ids = frozenset(
            item.strip().upper()
            for item in self.settings.ALLOWED_MPT_ISSUANCE_IDS.split(",")
            if item.strip()
        )
        self._replay_store = replay_store or build_replay_store(
            self.settings,
            redis_client=redis_client,
        )
        self._owned_redis_client: Any | None = None
        self._paychannel_redemption_task: asyncio.Task[None] | None = None
        self._paychannel_worker: PayChannelRedemptionWorker | None = None
        self._paychannel_service = paychannel_service
        payer_public_key = self.settings.PAYCHANNEL_PAYER_PUBLIC_KEY
        recipient_seed = self.settings.PAYCHANNEL_RECIPIENT_SEED
        if recipient_signer is not None and recipient_seed is not None:
            raise ValueError(
                "recipient_signer cannot be combined with PAYCHANNEL_RECIPIENT_SEED"
            )
        self._paychannel_recipient_signer = recipient_signer
        if recipient_seed is not None:
            self._paychannel_recipient_signer = LocalSeedRecipientSigner(
                recipient_seed.get_secret_value()
            )
        if self._paychannel_recipient_signer is not None:
            if (
                self._paychannel_recipient_signer.account
                != self.settings.MY_DESTINATION_ADDRESS
            ):
                raise ValueError(
                    "PayChannel recipient signer does not match MY_DESTINATION_ADDRESS"
                )
        paychannel_redemption_lease_seconds = max(
            self.settings.PAYCHANNEL_REDEEM_LEASE_SECONDS,
            self.settings.VALIDATION_TIMEOUT + 60,
        )
        if self._paychannel_service is None and payer_public_key is not None:
            if redis_client is None:
                redis_client = create_async_redis_client(
                    self.settings.REDIS_URL.get_secret_value()
                )
                self._owned_redis_client = redis_client
            paychannel_store = RedisPayChannelStore(redis_client)
            self._paychannel_service = PayChannelService(
                store=paychannel_store,
                replay_store=self._replay_store,
                challenge_secrets=self.settings.challenge_secrets(),
                network=self.settings.NETWORK_ID,
                recipient=self.settings.MY_DESTINATION_ADDRESS,
                payer_public_key=payer_public_key.get_secret_value(),
                open_submitter=self._submit_open_channel,
                ledger_verifier=self._verify_channel_ledger,
                channel_loader=self._load_paychannel_record,
                close_settler=(
                    self._settle_paychannel_claim
                    if self._paychannel_recipient_signer is not None
                    else None
                ),
                signer_authorizer=self._signer_is_authorized,
                current_ledger_index=self._get_latest_validated_ledger_sequence,
                minimum_settle_delay=self.settings.PAYCHANNEL_MIN_SETTLE_DELAY,
                settlement_margin_seconds=(
                    self.settings.PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS
                ),
                redemption_lease_seconds=paychannel_redemption_lease_seconds,
            )
            if self.settings.PAYCHANNEL_REDEEM_INTERVAL_SECONDS > 0:
                if self._paychannel_recipient_signer is None:
                    raise ValueError(
                        "PayChannel background redemption requires a recipient signer"
                    )
                self._paychannel_worker = PayChannelRedemptionWorker(
                    store=paychannel_store,
                    settler=self._settle_paychannel_claim,
                    network=self.settings.NETWORK_ID,
                    interval_seconds=(
                        self.settings.PAYCHANNEL_REDEEM_INTERVAL_SECONDS
                    ),
                    idle_close_seconds=self.settings.PAYCHANNEL_IDLE_CLOSE_SECONDS,
                    batch_size=self.settings.PAYCHANNEL_REDEEM_BATCH_SIZE,
                    lease_seconds=paychannel_redemption_lease_seconds,
                )
        if (
            self.settings.PAYCHANNEL_REDEEM_INTERVAL_SECONDS > 0
            and self._paychannel_worker is None
        ):
            raise ValueError(
                "PayChannel background redemption requires configured session support "
                "and a recipient signer"
            )

    async def start(self) -> None:
        """Start configured facilitator maintenance tasks once per service."""

        if self._paychannel_worker is None:
            return
        if (
            self._paychannel_redemption_task is None
            or self._paychannel_redemption_task.done()
        ):
            self._paychannel_redemption_task = asyncio.create_task(
                self._paychannel_worker.run_forever(),
                name="xrpl-mpp-paychannel-redemption",
            )

    async def aclose(self) -> None:
        """Close only Redis connections created directly by this service."""

        if self._paychannel_redemption_task is not None:
            self._paychannel_redemption_task.cancel()
            await asyncio.gather(
                self._paychannel_redemption_task,
                return_exceptions=True,
            )
            self._paychannel_redemption_task = None

        if self._owned_redis_client is not None:
            await self._owned_redis_client.aclose()
            self._owned_redis_client = None

    async def _client_request(self, request: Any) -> Any:
        return await asyncio.to_thread(self.client.request, request)

    @staticmethod
    def _verify_single_signer_signature(tx_dict: dict[str, Any]) -> tuple[str, str]:
        if tx_dict.get("Signers"):
            raise ValueError("Multisigned transactions are not supported")
        account = tx_dict.get("Account")
        signing_public_key = tx_dict.get("SigningPubKey")
        transaction_signature = tx_dict.get("TxnSignature")
        if not isinstance(account, str) or not account:
            raise ValueError("Transaction Account missing")
        if not isinstance(signing_public_key, str) or not signing_public_key:
            raise ValueError("Transaction SigningPubKey missing")
        if not isinstance(transaction_signature, str) or not transaction_signature:
            raise ValueError("Transaction signature missing")
        try:
            signing_address = derive_classic_address(signing_public_key)
            unsigned = dict(tx_dict)
            unsigned.pop("TxnSignature", None)
            message = bytes.fromhex(binarycodec.encode_for_signing(unsigned))
            valid = is_valid_message(
                message,
                bytes.fromhex(transaction_signature),
                signing_public_key,
            )
        except Exception as exc:
            raise ValueError("Transaction signature invalid") from exc
        if not valid:
            raise ValueError("Transaction signature invalid")
        return account, signing_address

    @staticmethod
    def _master_key_is_disabled(account_data: dict[str, Any]) -> bool:
        account_flags = account_data.get("account_flags")
        if isinstance(account_flags, dict) and (
            bool(account_flags.get("disableMasterKey"))
            or bool(account_flags.get("DisableMasterKey"))
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
        if signing_address == account:
            return
        response = await self._client_request(
            AccountInfo(account=account, ledger_index="validated")
        )
        result = getattr(response, "result", {})
        account_data = result.get("account_data") if isinstance(result, dict) else None
        if not isinstance(account_data, dict):
            raise ValueError("Unable to verify signing authority")
        authorized: set[str] = set()
        if not self._master_key_is_disabled(account_data):
            authorized.add(account)
        regular_key = account_data.get("RegularKey")
        if isinstance(regular_key, str) and regular_key.strip():
            authorized.add(regular_key.strip())
        if signing_address not in authorized:
            raise ValueError("SigningPubKey is not authorized for Account")

    async def _signer_is_authorized(
        self,
        *,
        account: str,
        signing_address: str,
    ) -> bool:
        try:
            await self._ensure_signing_address_authorized(
                account=account,
                signing_address=signing_address,
            )
        except ValueError:
            return False
        return True

    async def _decode_payment(self, signed_tx_blob: str) -> tuple[Payment, dict[str, Any]]:
        try:
            raw = binarycodec.decode(signed_tx_blob)
        except Exception as exc:
            raise ValueError("Could not decode signed XRPL transaction") from exc
        if raw.get("TransactionType") != "Payment":
            raise ValueError("TransactionType must be Payment")
        account, signing_address = self._verify_single_signer_signature(raw)
        await self._ensure_signing_address_authorized(
            account=account,
            signing_address=signing_address,
        )
        payment = Payment.from_xrpl(raw)
        if not payment.is_signed():
            raise ValueError("Transaction must be signed")
        return payment, raw

    @staticmethod
    def _normalize_issued_amount_fields(
        currency: Any,
        issuer: Any,
        raw_value: Any,
    ) -> NormalizedAmount:
        normalized_currency = normalize_currency_code(str(currency))
        normalized_issuer = str(issuer).strip()
        if not normalized_issuer:
            raise ValueError("Issued asset issuer missing")
        try:
            value = Decimal(str(raw_value))
        except InvalidOperation as exc:
            raise ValueError("Issued asset value invalid") from exc
        if value < 0:
            raise ValueError("Negative issued asset amount not allowed")
        return NormalizedAmount(
            asset=AssetKey(code=normalized_currency, issuer=normalized_issuer),
            value=value,
        )

    def _normalize_amount(self, amount: Any) -> NormalizedAmount:
        if isinstance(amount, int | str):
            if amount == "unavailable":
                raise ValueError("Delivered amount unavailable")
            try:
                drops = int(amount)
            except (TypeError, ValueError) as exc:
                raise ValueError("XRP amount must be an integer drops value") from exc
            if drops < 0:
                raise ValueError("Negative XRP amount not allowed")
            return NormalizedAmount(
                asset=AssetKey(code=XRP_CODE),
                value=Decimal(drops),
                drops=drops,
            )
        if isinstance(amount, dict):
            mpt_id = amount.get("mpt_issuance_id") or amount.get("mptIssuanceId")
            if mpt_id is not None:
                try:
                    value = Decimal(str(amount.get("value")))
                except InvalidOperation as exc:
                    raise ValueError("MPT amount is invalid") from exc
                if value < 0:
                    raise ValueError("Negative MPT amount not allowed")
                return NormalizedAmount(
                    asset=AssetKey(
                        code="MPT",
                        mpt_issuance_id=str(mpt_id).upper(),
                    ),
                    value=value,
                )
            return self._normalize_issued_amount_fields(
                amount.get("currency", ""),
                amount.get("issuer", ""),
                amount.get("value"),
            )
        if hasattr(amount, "mpt_issuance_id") and hasattr(amount, "value"):
            return self._normalize_amount(
                {
                    "mpt_issuance_id": getattr(amount, "mpt_issuance_id"),
                    "value": getattr(amount, "value"),
                }
            )
        if all(hasattr(amount, field) for field in ("currency", "issuer", "value")):
            return self._normalize_issued_amount_fields(
                getattr(amount, "currency"),
                getattr(amount, "issuer"),
                getattr(amount, "value"),
            )
        raise ValueError("Unsupported payment amount format")

    def _normalize_requested_amount(self, currency: str, amount: str) -> NormalizedAmount:
        parsed = parse_currency(currency)
        if parsed == XRP:
            if "." in amount:
                raise ValueError("XRP request amount must be integer drops")
            return self._normalize_amount(amount)
        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise ValueError("Requested amount is invalid") from exc
        if isinstance(parsed, IssuedCurrency):
            return NormalizedAmount(
                asset=AssetKey(
                    code=normalize_currency_code(parsed.currency),
                    issuer=parsed.issuer,
                ),
                value=value,
            )
        if isinstance(parsed, MPToken):
            return NormalizedAmount(
                asset=AssetKey(
                    code="MPT",
                    mpt_issuance_id=parsed.mpt_issuance_id.upper(),
                ),
                value=value,
            )
        raise ValueError("Unsupported XRPL currency")

    def _ensure_policy(self, payment: Payment, amount: NormalizedAmount) -> None:
        raw_flags = payment.flags or 0
        flags = int(raw_flags, 0) if isinstance(raw_flags, str) else int(raw_flags)
        if flags & TF_PARTIAL_PAYMENT:
            raise ValueError("Partial payments are not supported")
        if payment.destination != self.settings.MY_DESTINATION_ADDRESS:
            raise ValueError("Wrong destination address")
        if amount.asset.code == XRP_CODE:
            if amount.drops is None or amount.drops < self.settings.MIN_XRP_DROPS:
                raise ValueError("Payment below minimum amount")
            return
        if amount.asset.mpt_issuance_id is not None:
            if amount.asset.mpt_issuance_id not in self._allowed_mpt_ids:
                raise ValueError("Unsupported MPT issuance")
            return
        if amount.asset not in self._allowed_issued_assets:
            raise ValueError("Unsupported issued asset")

    def supported_currencies(self) -> list[str]:
        currencies = [XRP]
        for asset in self._supported_assets:
            if asset.issuer is None:
                continue
            currencies.append(
                serialize_currency(
                    IssuedCurrency(
                        currency=xrpl_currency_code(asset.code),
                        issuer=asset.issuer,
                    )
                )
            )
        currencies.extend(
            serialize_currency(MPToken(mpt_issuance_id=mpt_id))
            for mpt_id in sorted(self._allowed_mpt_ids)
        )
        return currencies

    def supported_methods(self) -> list[FacilitatorSupportedMethod]:
        intents = ["charge"]
        if self._paychannel_service is not None:
            intents.append("session")
        return [
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=intents,
                network=self.settings.NETWORK_ID,
                currencies=self.supported_currencies(),
                settlementMode=self.settings.SETTLEMENT_MODE,
            )
        ]

    async def _get_latest_validated_ledger_sequence(self) -> int:
        response = await self._client_request(Ledger(ledger_index="validated"))
        result = getattr(response, "result", {})
        try:
            return int(result["ledger_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Unable to determine current validated ledger") from exc

    @staticmethod
    def _extract_created_channel_id(metadata: dict[str, Any]) -> str:
        affected_nodes = metadata.get("AffectedNodes")
        if not isinstance(affected_nodes, list):
            raise ValueError("Validated PaymentChannelCreate metadata has no AffectedNodes")
        for wrapped in affected_nodes:
            if not isinstance(wrapped, dict):
                continue
            created = wrapped.get("CreatedNode")
            if not isinstance(created, dict) or created.get("LedgerEntryType") != "PayChannel":
                continue
            channel_id = created.get("LedgerIndex")
            if (
                isinstance(channel_id, str)
                and len(channel_id) == 64
                and all(character in "0123456789abcdefABCDEF" for character in channel_id)
            ):
                return channel_id.upper()
        raise ValueError("Validated PaymentChannelCreate metadata has no PayChannel node")

    async def _submit_open_channel(
        self,
        *,
        transaction_blob: str,
        transaction: Any,
    ) -> OpenChannelSubmission:
        try:
            tx_hash = PaymentChannelCreate.from_xrpl(dict(transaction)).get_hash().upper()
        except Exception as exc:
            raise ValueError("Could not calculate PaymentChannelCreate transaction hash") from exc

        try:
            response = await self._client_request(SubmitOnly(tx_blob=transaction_blob))
            self._ensure_submit_succeeded(response)
        except SubmissionRejectedError:
            raise
        except Exception as exc:
            raise SettlementPendingError(tx_hash) from exc
        for _ in range(self.settings.VALIDATION_TIMEOUT):
            try:
                tx_info = await self._client_request(Tx(transaction=tx_hash))
            except Exception as exc:
                logger.warning(
                    "paychannel_open_poll_failed",
                    tx_hash=tx_hash,
                    error=str(exc),
                )
                await asyncio.sleep(1)
                continue
            result = getattr(tx_info, "result", {})
            if isinstance(result, dict) and result.get("validated"):
                metadata = result.get("meta") or result.get("metaData")
                if not isinstance(metadata, dict):
                    raise SettlementPendingError(tx_hash)
                transaction_result = metadata.get("TransactionResult")
                if transaction_result is None:
                    raise SettlementPendingError(tx_hash)
                if transaction_result != "tesSUCCESS":
                    raise ValueError("PaymentChannelCreate did not validate successfully")
                try:
                    channel_id = self._extract_created_channel_id(metadata)
                except ValueError as exc:
                    raise SettlementPendingError(tx_hash) from exc
                return OpenChannelSubmission(
                    channelId=channel_id,
                    txHash=tx_hash,
                )
            await asyncio.sleep(1)
        raise SettlementPendingError(tx_hash)

    async def _verify_channel_ledger(
        self,
        *,
        record: PayChannelRecord,
        cumulative: str,
    ) -> str:
        response = await self._client_request(
            LedgerEntry(index=record.channel_id, ledger_index="validated")
        )
        result = getattr(response, "result", {})
        node = result.get("node") if isinstance(result, dict) else None
        if not isinstance(node, dict):
            raise PayChannelVerificationError(
                "CHANNEL_NOT_FOUND",
                "PayChannel does not exist in the validated ledger",
            )
        if node.get("Account") != record.payer or node.get("Destination") != record.recipient:
            raise PayChannelVerificationError(
                "CHANNEL_BINDING_MISMATCH",
                "Validated PayChannel parties do not match durable state",
            )
        configured_key = self.settings.PAYCHANNEL_PAYER_PUBLIC_KEY
        public_key = node.get("PublicKey")
        if (
            configured_key is None
            or not isinstance(public_key, str)
            or public_key.upper() != configured_key.get_secret_value().upper()
        ):
            raise PayChannelVerificationError(
                "PUBLIC_KEY_MISMATCH",
                "Validated PayChannel claim key does not match configuration",
            )
        amount = node.get("Amount")
        balance = node.get("Balance", "0")
        if (
            not isinstance(amount, str)
            or not amount.isascii()
            or not amount.isdigit()
            or not isinstance(balance, str)
            or not balance.isascii()
            or not balance.isdigit()
        ):
            raise PayChannelVerificationError(
                "INVALID_CHANNEL_STATE",
                "Validated PayChannel amount or balance is malformed",
            )
        if int(cumulative) > int(amount):
            raise PayChannelVerificationError(
                "CHANNEL_EXHAUSTED",
                "Cumulative claim exceeds validated PayChannel funding",
            )
        if int(cumulative) < int(balance):
            raise PayChannelVerificationError(
                "CUMULATIVE_REGRESSION",
                "Cumulative claim is below the validated on-ledger balance",
            )
        if int(balance) > int(amount):
            raise PayChannelVerificationError(
                "INVALID_CHANNEL_STATE",
                "Validated PayChannel balance exceeds funding",
            )
        settle_delay = node.get("SettleDelay")
        if (
            isinstance(settle_delay, bool)
            or not isinstance(settle_delay, int)
            or settle_delay < self.settings.PAYCHANNEL_MIN_SETTLE_DELAY
        ):
            raise PayChannelVerificationError(
                "SETTLE_DELAY_TOO_SHORT",
                "Validated PayChannel settle delay is below policy",
            )
        ripple_now = int(datetime.now(UTC).timestamp()) - 946_684_800
        settlement_deadline = (
            ripple_now + self.settings.PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS
        )
        for expiry_field in ("Expiration", "CancelAfter"):
            expiry = node.get(expiry_field)
            if expiry is not None and (
                isinstance(expiry, bool)
                or not isinstance(expiry, int)
                or expiry <= settlement_deadline
            ):
                raise PayChannelVerificationError(
                    "CHANNEL_CLOSING",
                    f"Validated PayChannel {expiry_field} is inside the settlement margin",
                )
        return amount

    async def _load_paychannel_record(self, *, channel_id: str) -> PayChannelRecord:
        response = await self._client_request(
            LedgerEntry(index=channel_id, ledger_index="validated")
        )
        result = getattr(response, "result", {})
        node = result.get("node") if isinstance(result, dict) else None
        if not isinstance(node, dict):
            raise PayChannelVerificationError(
                "CHANNEL_NOT_FOUND",
                "PayChannel does not exist in the validated ledger",
            )
        payer_key = self.settings.PAYCHANNEL_PAYER_PUBLIC_KEY
        if payer_key is None:
            raise PayChannelVerificationError(
                "PUBLIC_KEY_MISMATCH",
                "PayChannel payer public key is not configured",
            )
        expected_payer = derive_classic_address(payer_key.get_secret_value())
        if (
            node.get("Account") != expected_payer
            or node.get("Destination") != self.settings.MY_DESTINATION_ADDRESS
            or str(node.get("PublicKey", "")).upper()
            != payer_key.get_secret_value().upper()
        ):
            raise PayChannelVerificationError(
                "CHANNEL_BINDING_MISMATCH",
                "Validated PayChannel parties or public key do not match configuration",
            )
        funded = node.get("Amount")
        balance = node.get("Balance", "0")
        if (
            not isinstance(funded, str)
            or not funded.isascii()
            or not funded.isdigit()
            or int(funded) <= 0
            or not isinstance(balance, str)
            or not balance.isascii()
            or not balance.isdigit()
            or int(balance) > int(funded)
        ):
            raise PayChannelVerificationError(
                "INVALID_CHANNEL_STATE",
                "Validated PayChannel funding or balance is malformed",
            )
        settle_delay = node.get("SettleDelay")
        if (
            isinstance(settle_delay, bool)
            or not isinstance(settle_delay, int)
            or settle_delay < self.settings.PAYCHANNEL_MIN_SETTLE_DELAY
        ):
            raise PayChannelVerificationError(
                "SETTLE_DELAY_TOO_SHORT",
                "Validated PayChannel settle delay is below policy",
            )
        ripple_now = int(datetime.now(UTC).timestamp()) - 946_684_800
        deadline = ripple_now + self.settings.PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS
        for field in ("Expiration", "CancelAfter"):
            expiry = node.get(field)
            if expiry is not None and (
                isinstance(expiry, bool)
                or not isinstance(expiry, int)
                or expiry <= deadline
            ):
                raise PayChannelVerificationError(
                    "CHANNEL_CLOSING",
                    f"Validated PayChannel {field} is inside the settlement margin",
                )
        timestamp = int(datetime.now(UTC).timestamp() * 1_000)
        return PayChannelRecord(
            network=self.settings.NETWORK_ID,
            channel_id=channel_id,
            payer=expected_payer,
            recipient=self.settings.MY_DESTINATION_ADDRESS,
            funded=funded,
            cumulative=balance,
            signature="00" if int(balance) > 0 else "",
            redeemed=balance,
            created_at=timestamp,
            updated_at=timestamp,
            redeemed_at=timestamp if int(balance) > 0 else None,
            redemption_reference="ledger-import" if int(balance) > 0 else None,
        )

    async def _settle_paychannel_claim(self, *, record: PayChannelRecord) -> str:
        signer = self._paychannel_recipient_signer
        payer_key = self.settings.PAYCHANNEL_PAYER_PUBLIC_KEY
        if signer is None or payer_key is None:
            raise ValueError("PayChannel recipient signer is not configured")
        transaction = PaymentChannelClaim(
            account=signer.account,
            channel=record.channel_id,
            balance=record.cumulative,
            amount=record.cumulative,
            signature=record.signature,
            public_key=payer_key.get_secret_value().upper(),
            flags=0,
        )
        prepared = await asyncio.to_thread(autofill, transaction, self.client)
        fee = prepared.fee
        if (
            not isinstance(fee, str)
            or not fee.isascii()
            or not fee.isdigit()
            or int(fee) > self.settings.PAYCHANNEL_MAX_REDEMPTION_FEE_DROPS
        ):
            raise ValueError("PaymentChannelClaim fee exceeds the configured maximum")
        if prepared.last_ledger_sequence is None:
            raise ValueError("PaymentChannelClaim autofill returned no LastLedgerSequence")
        signed = await signer.sign_claim(prepared)
        if not isinstance(signed, PaymentChannelClaim):
            raise ValueError("Recipient signer returned an unexpected transaction type")
        expected_fields = prepared.to_xrpl()
        signed_fields = signed.to_xrpl()
        for field in ("SigningPubKey", "TxnSignature"):
            expected_fields.pop(field, None)
            signed_fields.pop(field, None)
        if signed_fields != expected_fields or not signed.is_signed():
            raise ValueError("Recipient signer changed the prepared PaymentChannelClaim")
        tx_hash = signed.get_hash().upper()
        response = await asyncio.to_thread(
            submit_and_wait,
            signed,
            self.client,
            autofill=False,
        )
        result = getattr(response, "result", {})
        metadata = (
            result.get("meta") or result.get("metaData")
            if isinstance(result, dict)
            else None
        )
        if (
            not isinstance(result, dict)
            or not isinstance(metadata, dict)
            or metadata.get("TransactionResult") != "tesSUCCESS"
            or result.get("validated") is not True
        ):
            raise ValueError("PaymentChannelClaim did not validate successfully")
        validated_hash = result.get("hash")
        if (
            not isinstance(validated_hash, str)
            or len(validated_hash) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in validated_hash
            )
        ):
            raise ValueError("Validated PaymentChannelClaim returned no transaction hash")
        if validated_hash.upper() != tx_hash:
            raise ValueError("Validated PaymentChannelClaim hash does not match signed claim")
        return tx_hash

    async def _ensure_payment_freshness(
        self,
        payment: Payment,
        *,
        challenge_expires: str | None = None,
    ) -> None:
        if payment.last_ledger_sequence is None:
            raise ValueError("Pull payment requires LastLedgerSequence")
        current = await self._get_latest_validated_ledger_sequence()
        last = int(payment.last_ledger_sequence)
        if last <= current:
            raise ValueError("Transaction LastLedgerSequence expired")
        if last > current + self.settings.MAX_PAYMENT_LEDGER_WINDOW:
            raise ValueError("Transaction LastLedgerSequence too far in the future")
        if challenge_expires is not None:
            expires_at = datetime.fromisoformat(challenge_expires.replace("Z", "+00:00"))
            remaining_seconds = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining_seconds <= 4:
                raise ValueError("Challenge leaves no XRPL ledger interval for settlement")
            expiry_cap = current + math.ceil(remaining_seconds / 4) + 4
            if last > expiry_cap:
                raise ValueError(
                    "Transaction LastLedgerSequence outlives the challenge expiry"
                )

    @staticmethod
    def _resolve_invoice_id(payment: Payment, expected_invoice_id: str) -> str:
        if not payment.invoice_id:
            raise ValueError("Payment carries no challenge-bound InvoiceID")
        if payment.invoice_id.upper() != expected_invoice_id.upper():
            raise ValueError("Payment InvoiceID does not match the challenge")
        return payment.invoice_id.upper()

    async def _validate_pull_payment(
        self,
        *,
        signed_tx_blob: str,
        expected_invoice_id: str,
        reserve: bool,
        challenge_expires: str | None = None,
    ) -> ValidatedPayment:
        payment, raw = await self._decode_payment(signed_tx_blob)
        invoice_id = self._resolve_invoice_id(payment, expected_invoice_id)
        amount = self._normalize_amount(payment.amount)
        self._ensure_policy(payment, amount)
        await self._ensure_payment_freshness(
            payment,
            challenge_expires=challenge_expires,
        )
        tx_hash = payment.get_hash().upper()
        blob_hash = hashlib.sha256(signed_tx_blob.encode("ascii")).hexdigest()
        reservation = None
        if reserve:
            retention_seconds = replay_retention_seconds(
                challenge_expires,
                validation_timeout_seconds=self.settings.VALIDATION_TIMEOUT,
            )
            reservation = await self._replay_store.reserve(
                invoice_id,
                blob_hash,
                retention_seconds=retention_seconds,
            )
        else:
            await self._replay_store.guard_available(invoice_id, blob_hash)
        return ValidatedPayment(
            tx=payment,
            raw=raw,
            invoice_id=invoice_id,
            tx_hash=tx_hash,
            amount=amount,
            signed_tx_blob=signed_tx_blob,
            replay_reservation=reservation,
        )

    def _assert_payment_matches_request(
        self,
        payment: ValidatedPayment,
        request: Any,
    ) -> None:
        expected = self._normalize_requested_amount(request.currency, request.amount)
        if payment.tx.destination != request.recipient:
            raise ValueError("Payment recipient does not match the challenge")
        if expected.asset != payment.amount.asset or expected.value != payment.amount.value:
            raise ValueError("Payment amount or currency does not match the challenge")
        details = request.method_details
        if details is None:
            return
        if details.destination_tag is not None and payment.tx.destination_tag != details.destination_tag:
            raise ValueError("Payment DestinationTag does not match the challenge")
        if details.source_tag is not None and payment.tx.source_tag != details.source_tag:
            raise ValueError("Payment SourceTag does not match the challenge")
        if details.memos is not None:
            expected_memos = _expected_memos(details.memos)
            actual_memos = payment.raw.get("Memos") or []
            if actual_memos != expected_memos:
                raise ValueError("Payment memos do not match the challenge")

    def _assert_source(self, credential: PaymentCredential, account: str) -> None:
        source = parse_xrpl_did(
            credential.source or "",
            expected_network=self.settings.NETWORK_ID,
        )
        if source.address != account:
            raise ValueError("Credential source does not match transaction Account")

    @staticmethod
    def _extract_delivered_amount(result: dict[str, Any]) -> Any:
        meta = result.get("meta") or result.get("metaData") or {}
        delivered = meta.get("delivered_amount") or meta.get("DeliveredAmount")
        if delivered is None:
            raise ValueError("Validated transaction missing delivered_amount")
        return delivered

    @classmethod
    def _ensure_submit_succeeded(cls, response: Any) -> dict[str, Any]:
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            raise SubmissionRejectedError("XRPL submission returned an invalid response")
        engine_result = result.get("engine_result")
        if engine_result not in ACCEPTED_SUBMIT_ENGINE_RESULTS:
            detail = result.get("engine_result_message") or result.get("error_message") or engine_result
            raise SubmissionRejectedError(
                f"XRPL submission rejected: {detail or 'unknown'}"
            )
        return result

    async def _settle_pull_payment(
        self,
        payment: ValidatedPayment,
    ) -> tuple[str, Literal["validated"]]:
        if payment.signed_tx_blob is None or payment.replay_reservation is None:
            raise ValueError("Pull payment is missing settlement state")
        try:
            response = await self._client_request(SubmitOnly(tx_blob=payment.signed_tx_blob))
            self._ensure_submit_succeeded(response)
        except SubmissionRejectedError:
            await self._replay_store.release_pending(payment.replay_reservation)
            raise
        except Exception as exc:
            raise SettlementPendingError(payment.tx_hash) from exc

        for _ in range(self.settings.VALIDATION_TIMEOUT):
            try:
                tx_info = await self._client_request(Tx(transaction=payment.tx_hash))
            except Exception as exc:
                logger.warning(
                    "payment_validation_poll_failed",
                    tx_hash=payment.tx_hash,
                    error=str(exc),
                )
                await asyncio.sleep(1)
                continue
            result = getattr(tx_info, "result", {})
            if isinstance(result, dict) and result.get("validated"):
                metadata = result.get("meta") or result.get("metaData")
                if not isinstance(metadata, dict):
                    raise SettlementPendingError(payment.tx_hash)
                if metadata.get("TransactionResult") != "tesSUCCESS":
                    await self._replay_store.release_pending(payment.replay_reservation)
                    raise ValueError("Payment did not validate successfully")
                try:
                    delivered = self._normalize_amount(self._extract_delivered_amount(result))
                except Exception:
                    await self._replay_store.mark_processed(payment.replay_reservation)
                    raise
                if delivered != payment.amount:
                    await self._replay_store.mark_processed(payment.replay_reservation)
                    raise ValueError("Validated transaction delivered an unexpected amount")
                await self._replay_store.mark_processed(payment.replay_reservation)
                return payment.tx_hash, "validated"
            await asyncio.sleep(1)
        raise SettlementPendingError(payment.tx_hash)

    async def _validate_push_payment(
        self,
        *,
        transaction_hash: str,
        expected_invoice_id: str,
        challenge_expires: str | None,
    ) -> tuple[ValidatedPayment, dict[str, Any]]:
        result: dict[str, Any] | None = None
        for _ in range(self.settings.VALIDATION_TIMEOUT):
            try:
                response = await self._client_request(
                    Tx(transaction=transaction_hash.upper())
                )
            except Exception as exc:
                logger.warning(
                    "push_payment_poll_failed",
                    tx_hash=transaction_hash.upper(),
                    error=str(exc),
                )
                await asyncio.sleep(1)
                continue
            candidate = getattr(response, "result", {})
            if isinstance(candidate, dict) and candidate.get("validated"):
                result = candidate
                break
            await asyncio.sleep(1)
        if result is None:
            raise SettlementPendingError(transaction_hash.upper())
        raw_candidate = result.get("tx_json") or result.get("transaction") or result
        if not isinstance(raw_candidate, dict):
            raise ValueError("Validated transaction payload is malformed")
        raw = dict(raw_candidate)
        raw.pop("hash", None)
        raw.pop("ledger_index", None)
        raw.pop("date", None)
        if raw.get("TransactionType") != "Payment":
            raise ValueError("TransactionType must be Payment")
        payment = Payment.from_xrpl(raw)
        calculated_hash = payment.get_hash().upper()
        if calculated_hash != transaction_hash.upper():
            raise ValueError("Validated transaction hash does not match the credential")
        metadata = result.get("meta") or result.get("metaData")
        if (
            not isinstance(metadata, dict)
            or metadata.get("TransactionResult") != "tesSUCCESS"
        ):
            raise ValueError("Payment did not validate successfully")
        if challenge_expires is None:
            raise ValueError("Push payment challenges must carry expires")
        tx_ledger_index = result.get("ledger_index")
        if isinstance(tx_ledger_index, bool) or not isinstance(tx_ledger_index, int):
            raise ValueError("Validated push transaction has no ledger_index")
        current_ledger = await self._get_latest_validated_ledger_sequence()
        expires_at = datetime.fromisoformat(challenge_expires.replace("Z", "+00:00"))
        issued_not_before = expires_at - timedelta(
            seconds=self.settings.MPP_CHALLENGE_TTL_SECONDS
        )
        now = datetime.now(UTC)
        if issued_not_before > now:
            raise ValueError("Push challenge validity exceeds facilitator policy")
        elapsed_ledgers = math.ceil((now - issued_not_before).total_seconds() / 4)
        earliest_allowed = current_ledger - elapsed_ledgers - 4
        if tx_ledger_index < earliest_allowed:
            raise ValueError("Push transaction predates the challenge window")
        if payment.last_ledger_sequence is not None:
            remaining_seconds = (expires_at - now).total_seconds()
            if remaining_seconds <= 0:
                raise ValueError("Challenge expired")
            expiry_cap = current_ledger + math.ceil(remaining_seconds / 4) + 4
            if int(payment.last_ledger_sequence) > expiry_cap:
                raise ValueError(
                    "Transaction LastLedgerSequence outlives the challenge expiry"
                )
        invoice_id = self._resolve_invoice_id(payment, expected_invoice_id)
        amount = self._normalize_amount(payment.amount)
        self._ensure_policy(payment, amount)
        retention_seconds = replay_retention_seconds(
            challenge_expires,
            validation_timeout_seconds=self.settings.VALIDATION_TIMEOUT,
        )
        reservation = await self._replay_store.reserve(
            invoice_id,
            transaction_hash.upper(),
            retention_seconds=retention_seconds,
        )
        validated = ValidatedPayment(
            tx=payment,
            raw=raw,
            invoice_id=invoice_id,
            tx_hash=transaction_hash.upper(),
            amount=amount,
            replay_reservation=reservation,
        )
        return validated, result

    def _assert_credential(self, credential: PaymentCredential, *, intent: str) -> None:
        if credential.challenge.method != "xrpl" or credential.challenge.intent != intent:
            raise ValueError(f"Credential must use xrpl/{intent}")
        if intent == "charge" and credential.challenge.expires is None:
            raise ValueError("XRPL charge challenges must carry expires")
        if not verify_challenge_binding(
            credential.challenge,
            secrets=self.settings.challenge_secrets(),
        ):
            raise ValueError("Challenge binding invalid")
        if challenge_is_expired(credential.challenge):
            raise ValueError("Challenge expired")

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        self._assert_credential(credential, intent="charge")
        request = decode_challenge_request(credential.challenge)
        details = request.method_details
        network = details.network if details and details.network else self.settings.NETWORK_ID
        if network != self.settings.NETWORK_ID:
            raise ValueError("Challenge network does not match facilitator network")
        expected_invoice_id = (
            details.invoice_id if details and details.invoice_id else challenge_invoice_id(credential.challenge.id)
        )
        payload = decode_charge_payload(credential)

        if payload.type == "transaction":
            payment = await self._validate_pull_payment(
                signed_tx_blob=payload.blob,
                expected_invoice_id=expected_invoice_id,
                reserve=True,
                challenge_expires=credential.challenge.expires,
            )
            try:
                self._assert_payment_matches_request(payment, request)
                self._assert_source(credential, payment.tx.account)
            except Exception:
                if payment.replay_reservation is not None:
                    await self._replay_store.release_pending(payment.replay_reservation)
                raise
            tx_hash, settlement_status = await self._settle_pull_payment(payment)
        else:
            payment, result = await self._validate_push_payment(
                transaction_hash=payload.hash,
                expected_invoice_id=expected_invoice_id,
                challenge_expires=credential.challenge.expires,
            )
            try:
                self._assert_payment_matches_request(payment, request)
                self._assert_source(credential, payment.tx.account)
                delivered = self._normalize_amount(self._extract_delivered_amount(result))
                if delivered != payment.amount:
                    raise ValueError("Validated transaction delivered an unexpected amount")
                if payment.replay_reservation is None:
                    raise ValueError("Push payment is missing replay state")
                await self._replay_store.mark_processed(payment.replay_reservation)
            except Exception:
                if payment.replay_reservation is not None:
                    await self._replay_store.release_pending(payment.replay_reservation)
                raise
            tx_hash, settlement_status = payment.tx_hash, "validated"

        return PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp=_timestamp(),
            reference=tx_hash,
            challengeId=credential.challenge.id,
            network=network,
            payer=payment.tx.account,
            recipient=payment.tx.destination,
            invoiceId=payment.invoice_id,
            txHash=tx_hash,
            settlementStatus=settlement_status,
        )

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        if self._paychannel_service is None:
            raise ValueError("XRPL PayChannel support is not configured")
        result = await self._paychannel_service.verify(credential)
        return PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp=_timestamp(),
            reference=result.reference,
            challengeId=result.challenge_id,
            network=result.network,
            payer=result.payer,
            recipient=result.recipient,
            channelId=result.channel_id,
            cumulative=result.cumulative,
            action=result.action,
            txHash=result.tx_hash,
            settlementStatus="validated" if result.tx_hash else None,
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _expected_memos(memos: list[Any]) -> list[dict[str, dict[str, str]]]:
    rendered: list[dict[str, dict[str, str]]] = []
    for memo in memos:
        body: dict[str, str] = {}
        if memo.type:
            body["MemoType"] = memo.type.encode("utf-8").hex().upper()
        if memo.format:
            body["MemoFormat"] = memo.format.encode("utf-8").hex().upper()
        if memo.data:
            body["MemoData"] = memo.data.encode("utf-8").hex().upper()
        rendered.append({"Memo": body})
    return rendered
