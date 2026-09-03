from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from xrpl_mpp_core import PaymentChallenge, decode_challenge_request, parse_currency


class PaymentPolicyError(ValueError):
    """Raised before signing when automatic payment is not explicitly authorized."""


@dataclass(frozen=True, init=False)
class XRPLPaymentPolicy:
    """Static allowlist and spend ceiling for automatic XRPL payments.

    A policy is deliberately complete: automatic transports must verify the
    recipient, amount, and currency carried by every untrusted 402 challenge.
    Direct signer calls remain available for callers that authorize each
    challenge through their own interactive workflow.
    """

    expected_recipients: frozenset[str]
    max_amount: Decimal
    allowed_currencies: frozenset[str]
    max_challenge_validity_seconds: int

    def __init__(
        self,
        *,
        expected_recipients: str | Iterable[str],
        max_amount: str,
        allowed_currencies: Iterable[str],
        max_challenge_validity_seconds: int = 300,
    ) -> None:
        recipients = (
            frozenset({expected_recipients})
            if isinstance(expected_recipients, str)
            else frozenset(expected_recipients)
        )
        if isinstance(allowed_currencies, str):
            raise TypeError("allowed_currencies must be an iterable of currency strings")
        currencies = frozenset(allowed_currencies)
        if any(not isinstance(value, str) for value in recipients):
            raise TypeError("expected_recipients must contain only strings")
        if any(not isinstance(value, str) for value in currencies):
            raise TypeError("allowed_currencies must contain only strings")
        for currency in currencies:
            parse_currency(currency)

        try:
            ceiling = Decimal(max_amount)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("max_amount must be a non-negative decimal string") from exc
        if not ceiling.is_finite() or ceiling < 0:
            raise ValueError("max_amount must be a non-negative finite decimal string")
        if (
            isinstance(max_challenge_validity_seconds, bool)
            or not isinstance(max_challenge_validity_seconds, int)
            or max_challenge_validity_seconds <= 0
        ):
            raise ValueError("max_challenge_validity_seconds must be a positive integer")

        object.__setattr__(self, "expected_recipients", recipients)
        object.__setattr__(self, "max_amount", ceiling)
        object.__setattr__(self, "allowed_currencies", currencies)
        object.__setattr__(
            self,
            "max_challenge_validity_seconds",
            max_challenge_validity_seconds,
        )

    def authorize(self, challenge: PaymentChallenge) -> None:
        """Authorize one decoded challenge or raise before signing."""

        if challenge.method != "xrpl" or challenge.intent not in {"charge", "session"}:
            raise PaymentPolicyError("Automatic payment policy only authorizes XRPL payments")
        if challenge.expires is None:
            raise PaymentPolicyError(
                "Automatic payment policy requires an expiring payment challenge"
            )
        expiration = datetime.fromisoformat(challenge.expires.replace("Z", "+00:00"))
        remaining = (expiration - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise PaymentPolicyError("Payment challenge has expired")
        if remaining > self.max_challenge_validity_seconds:
            raise PaymentPolicyError(
                "Payment challenge validity window exceeds automatic payment policy"
            )
        request = decode_challenge_request(challenge)
        currency = request.currency or "XRP"
        if request.recipient not in self.expected_recipients:
            raise PaymentPolicyError(
                "Payment challenge recipient is not allowed by automatic payment policy"
            )
        try:
            amount = Decimal(request.amount)
        except InvalidOperation as exc:
            raise PaymentPolicyError("Payment challenge amount is invalid") from exc
        if amount > self.max_amount:
            raise PaymentPolicyError(
                "Payment challenge amount exceeds automatic payment policy max_amount"
            )
        if currency not in self.allowed_currencies:
            raise PaymentPolicyError(
                "Payment challenge currency is not allowed by automatic payment policy"
            )
