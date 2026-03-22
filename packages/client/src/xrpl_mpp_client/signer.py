from __future__ import annotations

import asyncio
from typing import Any

from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, sign
from xrpl.wallet import Wallet

from xrpl_mpp_core import (
    PaymentChallenge,
    PaymentCredential,
    PaymentReceipt,
    XRPLAmount,
    XRPLAsset,
    XRPLChargeCredentialPayload,
    XRPLSessionCredentialPayload,
    canonical_asset_identifier,
    decode_challenge_request,
    decode_payment_receipt,
    decode_session_payload,
    encode_payment_credential,
    extract_payment_challenges,
    parse_asset_identifier,
    render_payment_challenge,
    xrpl_currency_code,
)

WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"
AUTHORIZATION_HEADER = "Authorization"
PAYMENT_RECEIPT_HEADER = "Payment-Receipt"
SESSION_ID_HEADER = "X-MPP-Session-Id"


def select_payment_challenge(
    challenges: list[PaymentChallenge],
    *,
    intent: str | None = None,
    network: str | None = None,
    asset: str | XRPLAsset | None = None,
) -> PaymentChallenge:
    candidates = [challenge for challenge in challenges if challenge.method == "xrpl"]
    if intent is not None:
        candidates = [challenge for challenge in candidates if challenge.intent == intent]
    if network is not None or asset is not None:
        filtered: list[PaymentChallenge] = []
        for challenge in candidates:
            try:
                request = decode_challenge_request(challenge)
            except ValueError:
                continue
            if network is not None and request.method_details.network != network:
                continue
            if asset is not None:
                asset_identifier = (
                    canonical_asset_identifier(asset)
                    if isinstance(asset, XRPLAsset)
                    else canonical_asset_identifier(_asset_from_identifier(asset))
                )
                if request.currency != asset_identifier:
                    continue
            filtered.append(challenge)
        candidates = filtered
    if not candidates:
        raise ValueError("No matching XRPL MPP payment challenge found")
    return candidates[0]


class XRPLPaymentSigner:
    def __init__(
        self,
        wallet: Wallet,
        *,
        rpc_url: str = "https://s1.ripple.com:51234",
        network: str | None = None,
        client: JsonRpcClient | None = None,
        autofill_enabled: bool = True,
        default_fee: str = "12",
        default_sequence: int = 1,
        default_last_ledger_sequence: int | None = None,
    ) -> None:
        self.wallet = wallet
        self.network = network
        self._client = client or JsonRpcClient(rpc_url)
        self._autofill_enabled = autofill_enabled
        self._default_fee = default_fee
        self._default_sequence = default_sequence
        self._default_last_ledger_sequence = default_last_ledger_sequence

    def build_charge_credential(self, challenge: PaymentChallenge) -> PaymentCredential:
        request = decode_challenge_request(challenge)
        if challenge.intent != "charge":
            raise ValueError("Challenge intent must be charge")
        if self.network is not None and request.method_details.network != self.network:
            raise ValueError(
                f"Payment challenge network {request.method_details.network} does not match signer network {self.network}"
            )
        asset = _asset_from_identifier(request.currency)
        amount = _amount_from_identifier(asset, request.amount)
        signed_tx_blob = self.sign_payment(
            pay_to=request.recipient,
            asset=asset,
            amount=amount,
            invoice_id=request.method_details.invoice_id,
        )
        return PaymentCredential(
            challenge=challenge,
            payload=XRPLChargeCredentialPayload(signedTxBlob=signed_tx_blob).model_dump(
                by_alias=True,
                exclude_none=True,
            ),
        )

    async def build_charge_credential_async(self, challenge: PaymentChallenge) -> PaymentCredential:
        return await asyncio.to_thread(self.build_charge_credential, challenge)

    def build_session_open_credential(self, challenge: PaymentChallenge) -> PaymentCredential:
        request = decode_challenge_request(challenge)
        if challenge.intent != "session":
            raise ValueError("Challenge intent must be session")
        if self.network is not None and request.method_details.network != self.network:
            raise ValueError(
                f"Payment challenge network {request.method_details.network} does not match signer network {self.network}"
            )
        asset = _asset_from_identifier(request.currency)
        amount = _amount_from_identifier(asset, request.method_details.min_prepay_amount)
        signed_tx_blob = self.sign_payment(
            pay_to=request.recipient,
            asset=asset,
            amount=amount,
            invoice_id=request.method_details.session_id,
        )
        payload = XRPLSessionCredentialPayload(
            action="open",
            signedTxBlob=signed_tx_blob,
        )
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )

    async def build_session_open_credential_async(self, challenge: PaymentChallenge) -> PaymentCredential:
        return await asyncio.to_thread(self.build_session_open_credential, challenge)

    def build_session_use_credential(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        if challenge.intent != "session":
            raise ValueError("Challenge intent must be session")
        payload = XRPLSessionCredentialPayload(action="use", sessionToken=session_token)
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )

    async def build_session_use_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        return self.build_session_use_credential(challenge, session_token=session_token)

    def build_session_top_up_credential(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        request = decode_challenge_request(challenge)
        if challenge.intent != "session":
            raise ValueError("Challenge intent must be session")
        if self.network is not None and request.method_details.network != self.network:
            raise ValueError(
                f"Payment challenge network {request.method_details.network} does not match signer network {self.network}"
            )
        asset = _asset_from_identifier(request.currency)
        amount = _amount_from_identifier(asset, request.method_details.min_prepay_amount)
        signed_tx_blob = self.sign_payment(
            pay_to=request.recipient,
            asset=asset,
            amount=amount,
            invoice_id=request.method_details.session_id,
        )
        payload = XRPLSessionCredentialPayload(
            action="top_up",
            sessionToken=session_token,
            signedTxBlob=signed_tx_blob,
        )
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )

    async def build_session_top_up_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        return await asyncio.to_thread(
            self.build_session_top_up_credential,
            challenge,
            session_token=session_token,
        )

    def build_session_close_credential(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        if challenge.intent != "session":
            raise ValueError("Challenge intent must be session")
        payload = XRPLSessionCredentialPayload(action="close", sessionToken=session_token)
        return PaymentCredential(
            challenge=challenge,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )

    async def build_session_close_credential_async(
        self,
        challenge: PaymentChallenge,
        *,
        session_token: str,
    ) -> PaymentCredential:
        return self.build_session_close_credential(challenge, session_token=session_token)

    def sign_payment(
        self,
        *,
        pay_to: str,
        asset: XRPLAsset,
        amount: XRPLAmount,
        invoice_id: str,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
    ) -> str:
        payment_kwargs: dict[str, Any] = {
            "account": self.wallet.classic_address,
            "destination": pay_to,
            "amount": _to_xrpl_amount(asset, amount),
            "invoice_id": invoice_id,
        }

        if self._autofill_enabled:
            payment = Payment(**payment_kwargs)
            signed_payment = sign(autofill(payment, self._client), self.wallet)
            return signed_payment.blob()

        payment_kwargs["fee"] = fee or self._default_fee
        payment_kwargs["sequence"] = sequence if sequence is not None else self._default_sequence
        if last_ledger_sequence is not None:
            payment_kwargs["last_ledger_sequence"] = last_ledger_sequence
        elif self._default_last_ledger_sequence is not None:
            payment_kwargs["last_ledger_sequence"] = self._default_last_ledger_sequence
        payment = Payment(**payment_kwargs)
        signed_payment = sign(payment, self.wallet)
        return signed_payment.blob()


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


def _asset_from_identifier(identifier: str) -> XRPLAsset:
    asset = parse_asset_identifier(identifier)
    return XRPLAsset(code=asset.code, issuer=asset.issuer)


def _amount_from_identifier(asset: XRPLAsset, amount: str) -> XRPLAmount:
    if canonical_asset_identifier(asset) == "XRP:native":
        drops = int(amount)
        return XRPLAmount(value=str(drops), unit="drops", drops=drops)
    return XRPLAmount(value=amount, unit="issued")


def _to_xrpl_amount(asset: XRPLAsset, amount: XRPLAmount) -> str | IssuedCurrencyAmount:
    if amount.unit == "drops":
        return amount.value
    if asset.issuer is None:
        raise ValueError("Issued-asset payments require an issuer")
    return IssuedCurrencyAmount(
        currency=xrpl_currency_code(asset.code),
        issuer=asset.issuer,
        value=amount.value,
    )
