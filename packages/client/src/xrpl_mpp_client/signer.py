from __future__ import annotations

import asyncio
import ipaddress
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha512
from typing import Any, Literal
from urllib.parse import urlsplit

from xrpl.clients import JsonRpcClient
from xrpl.core import addresscodec, binarycodec
from xrpl.core.keypairs import sign as sign_message
from xrpl.models.amounts import IssuedCurrencyAmount, MPTAmount
from xrpl.models.requests import LedgerCurrent
from xrpl.models.transactions import Payment, PaymentChannelCreate
from xrpl.models.transactions.transaction import Memo
from xrpl.transaction import autofill, sign
from xrpl.wallet import Wallet

from xrpl_mpp_core import (
    PaymentChallenge,
    PaymentCredential,
    PaymentReceipt,
    XRPLChannelClosePayload,
    XRPLChannelOpenPayload,
    XRPLChannelVoucherPayload,
    XRPLHashCredentialPayload,
    XRPLMemo,
    XRPLNetwork,
    XRPLTransactionCredentialPayload,
    build_ledger_amount,
    build_xrpl_did,
    challenge_invoice_id,
    challenge_is_expired,
    decode_challenge_request,
    decode_payment_receipt,
    encode_payment_credential,
    extract_payment_challenges,
    parse_currency,
)
from xrpl_mpp_client.pathfinding import (
    XRPLIOUPathfindingPolicy,
    resolve_iou_payment,
)
from xrpl_mpp_client.policy import XRPLPaymentPolicy

WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"
AUTHORIZATION_HEADER = "Authorization"
PAYMENT_RECEIPT_HEADER = "Payment-Receipt"

# On-chain attribution tag used by Ripple's reference SDK. A challenge-provided
# sourceTag always wins because an XRPL transaction has only one SourceTag.
MPP_SOURCE_TAG = 593_184_257

XRPL_RPC_URLS: dict[XRPLNetwork, str] = {
    "mainnet": "https://s1.ripple.com:51234",
    "testnet": "https://s.altnet.rippletest.net:51234",
    "devnet": "https://s.devnet.rippletest.net:51234",
}
LEDGER_CLOSE_INTERVAL_SECONDS = 4
DEFAULT_MAX_FEE_DROPS = "1000"
PAYCHANNEL_LEDGER_SPACE_KEY = b"\x00\x78"


@dataclass(frozen=True, slots=True)
class PayChannelOpenBinding:
    """Identifiers deterministically bound to one signed channel-create blob."""

    channel_id: str
    tx_hash: str
    payer: str
    recipient: str
    funding_amount: str
    public_key: str


def derive_paychannel_open_binding(transaction_blob: str) -> PayChannelOpenBinding:
    """Derive the PayChannel ledger ID and transaction hash without trusting a receipt."""

    try:
        decoded = binarycodec.decode(transaction_blob)
        transaction = PaymentChannelCreate.from_xrpl(decoded)
    except Exception as exc:
        raise ValueError("transaction must be a signed PaymentChannelCreate blob") from exc
    if decoded.get("TransactionType") != "PaymentChannelCreate":
        raise ValueError("transaction must be a PaymentChannelCreate")
    if not transaction.is_signed():
        raise ValueError("transaction must be a signed PaymentChannelCreate blob")

    ticket_sequence = decoded.get("TicketSequence")
    sequence = ticket_sequence if ticket_sequence is not None else decoded.get("Sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or sequence > 0xFFFFFFFF
    ):
        raise ValueError("PaymentChannelCreate requires a valid Sequence or TicketSequence")
    payer = decoded.get("Account")
    recipient = decoded.get("Destination")
    if not isinstance(payer, str) or not isinstance(recipient, str):
        raise ValueError("PaymentChannelCreate Account and Destination are required")
    funding_amount = decoded.get("Amount")
    if (
        not isinstance(funding_amount, str)
        or not funding_amount.isascii()
        or not funding_amount.isdigit()
        or int(funding_amount) <= 0
    ):
        raise ValueError("PaymentChannelCreate Amount must be positive XRP drops")
    public_key = decoded.get("PublicKey")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("PaymentChannelCreate PublicKey is required")
    try:
        channel_preimage = b"".join(
            (
                PAYCHANNEL_LEDGER_SPACE_KEY,
                addresscodec.decode_classic_address(payer),
                addresscodec.decode_classic_address(recipient),
                sequence.to_bytes(4, byteorder="big", signed=False),
            )
        )
    except Exception as exc:
        raise ValueError("PaymentChannelCreate parties are invalid XRPL addresses") from exc
    channel_id = sha512(channel_preimage).digest()[:32].hex().upper()
    return PayChannelOpenBinding(
        channel_id=channel_id,
        tx_hash=transaction.get_hash().upper(),
        payer=payer,
        recipient=recipient,
        funding_amount=funding_amount,
        public_key=public_key.upper(),
    )


def select_payment_challenge(
    challenges: list[PaymentChallenge],
    *,
    intent: str | None = None,
    network: XRPLNetwork | None = None,
    currency: str | None = None,
) -> PaymentChallenge:
    """Select an XRPL challenge, optionally filtering its decoded terms."""

    candidates = [challenge for challenge in challenges if challenge.method == "xrpl"]
    if intent is not None:
        candidates = [challenge for challenge in candidates if challenge.intent == intent]
    if network is not None or currency is not None:
        filtered: list[PaymentChallenge] = []
        for challenge in candidates:
            try:
                request = decode_challenge_request(challenge)
            except ValueError:
                continue
            details = request.method_details
            if network is not None and details is not None and details.network not in {None, network}:
                continue
            if currency is not None and request.currency != currency:
                continue
            filtered.append(challenge)
        candidates = filtered
    if not candidates:
        raise ValueError("No matching XRPL MPP payment challenge found")
    return candidates[0]


class XRPLPaymentSigner:
    """Create XRPL 0.2 charge and PayChannel credentials.

    Client policy guardrails are evaluated before any ledger call or signature.
    Direct calls may use partial guardrails because the call itself can be the
    user's authorization step. Automatic transports require a complete
    :class:`XRPLPaymentPolicy`.
    """

    def __init__(
        self,
        wallet: Wallet,
        *,
        rpc_url: str | None = None,
        network: XRPLNetwork = "mainnet",
        client: JsonRpcClient | None = None,
        autofill_enabled: bool = True,
        default_fee: str = "12",
        max_fee_drops: str = DEFAULT_MAX_FEE_DROPS,
        default_sequence: int = 1,
        default_last_ledger_sequence: int | None = None,
        allow_insecure_rpc: bool = False,
        iou_pathfinding_policy: XRPLIOUPathfindingPolicy | None = None,
        expected_recipient: str | Iterable[str] | None = None,
        max_amount: str | None = None,
        allowed_currencies: Iterable[str] | None = None,
        payment_policy: XRPLPaymentPolicy | None = None,
    ) -> None:
        if network not in {"mainnet", "testnet", "devnet"}:
            raise ValueError("network must be mainnet, testnet, or devnet")
        self.wallet = wallet
        self.network: XRPLNetwork = network
        configured_rpc_url = XRPL_RPC_URLS[network] if rpc_url is None else rpc_url
        validate_xrpl_rpc_url(configured_rpc_url, allow_insecure=allow_insecure_rpc)
        client_url = getattr(client, "url", None) if client is not None else None
        if isinstance(client_url, str):
            validate_xrpl_rpc_url(client_url, allow_insecure=allow_insecure_rpc)
            self.rpc_url = client_url
        else:
            self.rpc_url = configured_rpc_url
        self._client = client or JsonRpcClient(self.rpc_url)
        self._autofill_enabled = autofill_enabled
        self._default_fee = default_fee
        self._max_fee_drops = _parse_fee_drops(max_fee_drops, name="max_fee_drops")
        default_fee_drops = _parse_fee_drops(default_fee, name="default_fee")
        if default_fee_drops > self._max_fee_drops:
            raise ValueError("default_fee cannot exceed max_fee_drops")
        self._default_sequence = default_sequence
        self._default_last_ledger_sequence = default_last_ledger_sequence
        self._iou_pathfinding_policy = iou_pathfinding_policy
        if payment_policy is not None and any(
            value is not None for value in (expected_recipient, max_amount, allowed_currencies)
        ):
            raise ValueError(
                "payment_policy cannot be combined with expected_recipient, max_amount, "
                "or allowed_currencies"
            )
        if payment_policy is not None:
            expected_recipient = payment_policy.expected_recipients
            max_amount = str(payment_policy.max_amount)
            allowed_currencies = payment_policy.allowed_currencies

        if isinstance(expected_recipient, str):
            self._expected_recipients = frozenset({expected_recipient})
        else:
            self._expected_recipients = (
                frozenset(expected_recipient) if expected_recipient is not None else None
            )
        try:
            self._max_amount = Decimal(max_amount) if max_amount is not None else None
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("max_amount must be a decimal string") from exc
        if self._max_amount is not None and (
            not self._max_amount.is_finite() or self._max_amount < 0
        ):
            raise ValueError("max_amount must be a non-negative finite decimal string")
        self._allowed_currencies = (
            frozenset(allowed_currencies) if allowed_currencies is not None else None
        )
        self._automatic_payment_policy = payment_policy
        if (
            self._automatic_payment_policy is None
            and self._expected_recipients is not None
            and self._max_amount is not None
            and self._allowed_currencies is not None
        ):
            self._automatic_payment_policy = XRPLPaymentPolicy(
                expected_recipients=self._expected_recipients,
                max_amount=str(self._max_amount),
                allowed_currencies=self._allowed_currencies,
            )

    @property
    def automatic_payment_policy(self) -> XRPLPaymentPolicy | None:
        """Complete policy suitable for unattended transport signing, if configured."""

        return self._automatic_payment_policy

    def build_charge_credential(self, challenge: PaymentChallenge) -> PaymentCredential:
        request = self._validate_charge_challenge(challenge)
        details = request.method_details
        invoice_id = (
            details.invoice_id if details is not None and details.invoice_id else challenge_invoice_id(challenge.id)
        )
        blob = self.sign_payment(
            pay_to=request.recipient,
            currency=request.currency,
            amount=request.amount,
            invoice_id=invoice_id,
            destination_tag=details.destination_tag if details is not None else None,
            source_tag=details.source_tag if details is not None else None,
            memos=details.memos if details is not None else None,
            challenge_expires=challenge.expires,
        )
        payload = XRPLTransactionCredentialPayload(type="transaction", blob=blob)
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
            source=self._source_did(),
        )

    async def build_charge_credential_async(self, challenge: PaymentChallenge) -> PaymentCredential:
        return await asyncio.to_thread(self.build_charge_credential, challenge)

    def build_hash_credential(
        self,
        challenge: PaymentChallenge,
        *,
        transaction_hash: str,
    ) -> PaymentCredential:
        """Build a push-mode credential after the caller confirms settlement."""

        self._validate_charge_challenge(challenge)
        payload = XRPLHashCredentialPayload(type="hash", hash=transaction_hash)
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True),
            source=self._source_did(),
        )

    def build_session_open_credential(
        self,
        challenge: PaymentChallenge,
        *,
        open_transaction: str,
    ) -> PaymentCredential:
        request = self._validate_session_challenge(challenge)
        if request.channel_id != "":
            raise ValueError("an open challenge must carry an empty channelId")
        binding = derive_paychannel_open_binding(open_transaction)
        if binding.payer != self.wallet.classic_address:
            raise ValueError("PaymentChannelCreate payer does not match the signer wallet")
        if binding.recipient != request.recipient:
            raise ValueError(
                "PaymentChannelCreate recipient does not match the open challenge"
            )
        if binding.public_key != self.wallet.public_key.upper():
            raise ValueError("PaymentChannelCreate claim key does not match the signer wallet")
        if int(request.amount) > int(binding.funding_amount):
            raise ValueError("Initial cumulative claim exceeds PaymentChannelCreate funding")
        signature = self.sign_channel_claim(binding.channel_id, request.amount)
        payload = XRPLChannelOpenPayload(
            action="open",
            transaction=open_transaction,
            amount=request.amount,
            signature=signature,
        )
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True),
            source=self._source_did(),
        )

    async def build_session_open_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        open_transaction: str,
    ) -> PaymentCredential:
        return await asyncio.to_thread(
            self.build_session_open_credential,
            challenge,
            open_transaction=open_transaction,
        )

    def build_session_voucher_credential(
        self,
        challenge: PaymentChallenge,
        *,
        cumulative_amount: str | None = None,
    ) -> PaymentCredential:
        return self._build_session_claim_credential(
            challenge,
            action="voucher",
            cumulative_amount=cumulative_amount,
        )

    async def build_session_voucher_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        cumulative_amount: str | None = None,
    ) -> PaymentCredential:
        return await asyncio.to_thread(
            self.build_session_voucher_credential,
            challenge,
            cumulative_amount=cumulative_amount,
        )

    def build_session_close_credential(
        self,
        challenge: PaymentChallenge,
        *,
        cumulative_amount: str | None = None,
    ) -> PaymentCredential:
        return self._build_session_claim_credential(
            challenge,
            action="close",
            cumulative_amount=cumulative_amount,
        )

    async def build_session_close_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        cumulative_amount: str | None = None,
    ) -> PaymentCredential:
        return await asyncio.to_thread(
            self.build_session_close_credential,
            challenge,
            cumulative_amount=cumulative_amount,
        )

    def _build_session_claim_credential(
        self,
        challenge: PaymentChallenge,
        *,
        action: Literal["voucher", "close"],
        cumulative_amount: str | None,
    ) -> PaymentCredential:
        request = self._validate_session_challenge(challenge)
        if not request.channel_id:
            raise ValueError(f"channelId is required for action {action}")
        previous = int(request.method_details.cumulative_amount or "0") if request.method_details else 0
        cumulative = str(previous + int(request.amount)) if cumulative_amount is None else cumulative_amount
        if not cumulative.isascii() or not cumulative.isdigit():
            raise ValueError("cumulative_amount must be an unsigned drops string")
        if int(cumulative) < previous + int(request.amount):
            raise ValueError("cumulative_amount does not cover the requested increment")
        signature = self.sign_channel_claim(request.channel_id, cumulative)
        payload: XRPLChannelVoucherPayload | XRPLChannelClosePayload
        if action == "voucher":
            payload = XRPLChannelVoucherPayload(
                action="voucher",
                channelId=request.channel_id,
                amount=cumulative,
                signature=signature,
            )
        else:
            payload = XRPLChannelClosePayload(
                action="close",
                channelId=request.channel_id,
                amount=cumulative,
                signature=signature,
            )
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True),
            source=self._source_did(),
        )

    def sign_payment(
        self,
        *,
        pay_to: str,
        currency: str,
        amount: str,
        invoice_id: str,
        destination_tag: int | None = None,
        source_tag: int | None = None,
        memos: list[XRPLMemo] | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
        challenge_expires: str | None = None,
    ) -> str:
        parsed_currency = parse_currency(currency)
        ledger_amount = build_ledger_amount(amount, parsed_currency)
        if isinstance(ledger_amount, dict):
            if "mpt_issuance_id" in ledger_amount:
                xrpl_amount: str | IssuedCurrencyAmount | MPTAmount = MPTAmount(**ledger_amount)
            else:
                xrpl_amount = IssuedCurrencyAmount(**ledger_amount)
        else:
            xrpl_amount = ledger_amount

        paths = None
        send_max = None
        if isinstance(xrpl_amount, IssuedCurrencyAmount):
            if self.wallet.classic_address == xrpl_amount.issuer:
                # Issuers must omit SendMax: otherwise an adversarial trust-line
                # configuration can cause more tokens to be issued than Amount.
                pass
            elif self._iou_pathfinding_policy is None:
                # Safe direct-only default. This cannot spend more than the
                # exact destination amount and deliberately fails when a
                # transfer fee or cross-currency route would require more.
                send_max = xrpl_amount
            else:
                resolved = resolve_iou_payment(
                    client=self._client,
                    sender=self.wallet.classic_address,
                    recipient=pay_to,
                    destination_amount=xrpl_amount,
                    policy=self._iou_pathfinding_policy,
                )
                paths = resolved.paths
                send_max = resolved.send_max

        payment_kwargs: dict[str, Any] = {
            "account": self.wallet.classic_address,
            "destination": pay_to,
            "amount": xrpl_amount,
            "flags": 0,
            "invoice_id": invoice_id,
            "source_tag": source_tag if source_tag is not None else MPP_SOURCE_TAG,
        }
        if paths is not None:
            payment_kwargs["paths"] = paths
        if send_max is not None:
            payment_kwargs["send_max"] = send_max
        if destination_tag is not None:
            payment_kwargs["destination_tag"] = destination_tag
        if memos:
            payment_kwargs["memos"] = [_encode_memo(memo) for memo in memos]
        return self._sign_transaction(
            Payment(**payment_kwargs),
            fee=fee,
            sequence=sequence,
            last_ledger_sequence=last_ledger_sequence,
            challenge_expires=challenge_expires,
        )

    def sign_channel_create(
        self,
        *,
        destination: str,
        funding_amount: str,
        settle_delay: int,
        public_key: str | None = None,
        cancel_after: int | None = None,
        destination_tag: int | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
        challenge_expires: str | None = None,
    ) -> str:
        """Prepare and sign a pull-mode ``PaymentChannelCreate`` blob."""

        if not funding_amount.isascii() or not funding_amount.isdigit() or int(funding_amount) <= 0:
            raise ValueError("funding_amount must be a positive drops string")
        if isinstance(settle_delay, bool) or settle_delay < 0:
            raise ValueError("settle_delay must be a non-negative integer")
        transaction = PaymentChannelCreate(
            account=self.wallet.classic_address,
            destination=destination,
            amount=funding_amount,
            settle_delay=settle_delay,
            public_key=public_key or self.wallet.public_key,
            source_tag=MPP_SOURCE_TAG,
            cancel_after=cancel_after,
            destination_tag=destination_tag,
        )
        return self._sign_transaction(
            transaction,
            fee=fee,
            sequence=sequence,
            last_ledger_sequence=last_ledger_sequence,
            challenge_expires=challenge_expires,
        )

    async def sign_channel_create_async(
        self,
        *,
        destination: str,
        funding_amount: str,
        settle_delay: int,
        public_key: str | None = None,
        cancel_after: int | None = None,
        destination_tag: int | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
        challenge_expires: str | None = None,
    ) -> str:
        """Prepare and sign a channel-create blob without blocking an async loop.

        ``xrpl-py`` exposes transaction autofill through a synchronous wrapper
        that owns an event loop. Run the complete synchronous signing operation
        in a worker so async callers never nest ``asyncio.run()``.
        """

        return await asyncio.to_thread(
            self.sign_channel_create,
            destination=destination,
            funding_amount=funding_amount,
            settle_delay=settle_delay,
            public_key=public_key,
            cancel_after=cancel_after,
            destination_tag=destination_tag,
            fee=fee,
            sequence=sequence,
            last_ledger_sequence=last_ledger_sequence,
            challenge_expires=challenge_expires,
        )

    def sign_channel_claim(self, channel_id: str, cumulative_amount: str) -> str:
        if len(channel_id) != 64:
            raise ValueError("channel_id must be 64 hexadecimal characters")
        if not cumulative_amount.isascii() or not cumulative_amount.isdigit():
            raise ValueError("cumulative_amount must be an unsigned drops string")
        try:
            message = bytes.fromhex(
                binarycodec.encode_for_signing_claim(
                    {"channel": channel_id, "amount": cumulative_amount}
                )
            )
        except ValueError as exc:
            raise ValueError("channel_id must be 64 hexadecimal characters") from exc
        return sign_message(message, self.wallet.private_key)

    def _sign_transaction(
        self,
        transaction: Payment | PaymentChannelCreate,
        *,
        fee: str | None,
        sequence: int | None,
        last_ledger_sequence: int | None,
        challenge_expires: str | None,
    ) -> str:
        if fee is not None:
            self._validate_fee(fee)
        if self._autofill_enabled:
            original_updates = transaction.to_dict()
            initial_updates = dict(original_updates)
            if fee is not None:
                initial_updates["fee"] = fee
            if sequence is not None:
                initial_updates["sequence"] = sequence
            if last_ledger_sequence is not None:
                initial_updates["last_ledger_sequence"] = last_ledger_sequence
            transaction_type = type(transaction)
            prepared = autofill(transaction_type.from_dict(initial_updates), self._client)

            # Explicit caller fields remain authoritative even if a compromised
            # RPC/autofill implementation attempts to replace them.
            autofilled_updates = prepared.to_dict()
            prepared_updates = dict(original_updates)
            for autofill_field in (
                "fee",
                "sequence",
                "last_ledger_sequence",
                "network_id",
            ):
                if autofill_field in autofilled_updates:
                    prepared_updates[autofill_field] = autofilled_updates[autofill_field]
            if fee is not None:
                prepared_updates["fee"] = fee
            if sequence is not None:
                prepared_updates["sequence"] = sequence
            if last_ledger_sequence is not None:
                prepared_updates["last_ledger_sequence"] = last_ledger_sequence
            if challenge_expires is not None:
                current_ledger_sequence = self._read_current_ledger_sequence()
                expiry_cap = last_ledger_sequence_from_expires(
                    current_ledger_sequence=current_ledger_sequence,
                    expires=challenge_expires,
                )
                existing = prepared_updates.get("last_ledger_sequence")
                if existing is None or expiry_cap < existing:
                    prepared_updates["last_ledger_sequence"] = expiry_cap
            prepared = transaction_type.from_dict(prepared_updates)
            self._validate_fee(prepared.fee)
            return sign(prepared, self.wallet).blob()

        updates: dict[str, Any] = {
            **transaction.to_dict(),
            "fee": fee if fee is not None else self._default_fee,
            "sequence": sequence if sequence is not None else self._default_sequence,
        }
        resolved_last_ledger = (
            last_ledger_sequence
            if last_ledger_sequence is not None
            else self._default_last_ledger_sequence
        )
        if resolved_last_ledger is not None:
            updates["last_ledger_sequence"] = resolved_last_ledger
        transaction_type = type(transaction)
        prepared = transaction_type.from_dict(updates)
        self._validate_fee(prepared.fee)
        return sign(prepared, self.wallet).blob()

    def _validate_fee(self, fee: str | None) -> None:
        fee_drops = _parse_fee_drops(fee, name="transaction fee")
        if fee_drops > self._max_fee_drops:
            raise ValueError(
                f"Refusing to sign XRPL transaction fee {fee_drops} drops; "
                f"configured maximum is {self._max_fee_drops} drops"
            )

    def _read_current_ledger_sequence(self) -> int:
        response = self._client.request(LedgerCurrent())
        result = response.result
        sequence = result.get("ledger_current_index") if isinstance(result, dict) else None
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("XRPL ledger_current did not return a valid ledger index")
        return sequence

    def _validate_charge_challenge(self, challenge: PaymentChallenge):
        if challenge.method != "xrpl":
            raise ValueError("Challenge method must be xrpl")
        if challenge.intent != "charge":
            raise ValueError("Challenge intent must be charge")
        if challenge_is_expired(challenge):
            raise ValueError("Refusing to sign an expired MPP challenge")
        if self._automatic_payment_policy is not None:
            self._automatic_payment_policy.authorize(challenge)
        request = decode_challenge_request(challenge)
        details = request.method_details
        if details is not None and details.network not in {None, self.network}:
            raise ValueError(
                f"Payment challenge network {details.network} does not match signer network {self.network}"
            )
        if self._expected_recipients is not None and request.recipient not in self._expected_recipients:
            raise ValueError("Payment challenge recipient is not allowed by signer policy")
        try:
            amount = Decimal(request.amount)
        except InvalidOperation as exc:
            raise ValueError("Payment challenge amount is invalid") from exc
        if self._max_amount is not None and amount > self._max_amount:
            raise ValueError("Payment challenge amount exceeds signer max_amount")
        if self._allowed_currencies is not None and request.currency not in self._allowed_currencies:
            raise ValueError("Payment challenge currency is not allowed by signer policy")
        parse_currency(request.currency)
        return request

    def _validate_session_challenge(self, challenge: PaymentChallenge):
        if challenge.method != "xrpl":
            raise ValueError("Challenge method must be xrpl")
        if challenge.intent != "session":
            raise ValueError("Challenge intent must be session")
        if challenge_is_expired(challenge):
            raise ValueError("Refusing to sign an expired MPP challenge")
        if self._automatic_payment_policy is not None:
            self._automatic_payment_policy.authorize(challenge)
        request = decode_challenge_request(challenge)
        if request.currency not in {None, "XRP"}:
            raise ValueError("XRPL PayChannels are XRP-only")
        details = request.method_details
        if details is not None and details.network not in {None, self.network}:
            raise ValueError(
                f"Payment challenge network {details.network} does not match signer network {self.network}"
            )
        if self._expected_recipients is not None and request.recipient not in self._expected_recipients:
            raise ValueError("Payment challenge recipient is not allowed by signer policy")
        if self._max_amount is not None and Decimal(request.amount) > self._max_amount:
            raise ValueError("Payment challenge amount exceeds signer max_amount")
        if self._allowed_currencies is not None and "XRP" not in self._allowed_currencies:
            raise ValueError("Payment challenge currency is not allowed by signer policy")
        return request

    def _source_did(self) -> str:
        return build_xrpl_did(network=self.network, address=self.wallet.classic_address)


def build_payment_authorization(credential: PaymentCredential) -> str:
    return f"Payment {encode_payment_credential(credential)}"


def decode_payment_challenges_response(headers: Any) -> list[PaymentChallenge]:
    return extract_payment_challenges(headers)


def decode_payment_receipt_header(headers: Any) -> PaymentReceipt | None:
    response_header = headers.get(PAYMENT_RECEIPT_HEADER)
    if response_header is None:
        response_header = headers.get(PAYMENT_RECEIPT_HEADER.lower())
    if not response_header:
        return None
    return decode_payment_receipt(response_header)


def _encode_memo(memo: XRPLMemo) -> Memo:
    def to_hex(value: str | None) -> str | None:
        return value.encode("utf-8").hex().upper() if value else None

    return Memo(
        memo_type=to_hex(memo.type),
        memo_format=to_hex(memo.format),
        memo_data=to_hex(memo.data),
    )


def last_ledger_sequence_from_expires(
    *,
    current_ledger_sequence: int,
    expires: str,
    now: datetime | None = None,
) -> int:
    """Map challenge wall-clock expiry to the latest usable XRPL ledger."""

    if (
        isinstance(current_ledger_sequence, bool)
        or not isinstance(current_ledger_sequence, int)
        or current_ledger_sequence < 1
    ):
        raise ValueError("current_ledger_sequence must be a positive integer")
    expiration = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    active_now = now or datetime.now(UTC)
    if expiration.tzinfo is None or active_now.tzinfo is None:
        raise ValueError("expires and now must include a time-zone offset")
    remaining_seconds = (expiration - active_now).total_seconds()
    if remaining_seconds <= LEDGER_CLOSE_INTERVAL_SECONDS:
        raise ValueError(
            "Payment challenge leaves less than one XRPL ledger interval before expiry"
        )
    return current_ledger_sequence + math.ceil(
        remaining_seconds / LEDGER_CLOSE_INTERVAL_SECONDS
    )


def validate_xrpl_rpc_url(rpc_url: str, *, allow_insecure: bool = False) -> None:
    """Require authenticated RPC transport, with an explicit loopback-only escape hatch."""

    try:
        parsed = urlsplit(rpc_url)
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("rpc_url is malformed") from exc
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("rpc_url must be an absolute URL without credentials")
    if parsed.scheme == "https":
        return

    normalized_host = hostname.rstrip(".").lower()
    is_loopback = normalized_host == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and allow_insecure and is_loopback:
        return
    raise ValueError(
        "rpc_url must use HTTPS; loopback HTTP requires allow_insecure_rpc=True"
    )


def _parse_fee_drops(value: str | None, *, name: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or int(value) <= 0
    ):
        raise ValueError(f"{name} must be a positive drops string")
    return int(value)
