from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from xrpl.models.transactions import PaymentChannelClaim
from xrpl.transaction import sign
from xrpl.wallet import Wallet


@runtime_checkable
class RecipientSigner(Protocol):
    """Least-authority signing boundary for recipient PayChannel claims.

    Production implementations can delegate this operation to a KMS, HSM, or
    remote signing service. The facilitator prepares the complete transaction
    and rejects any signer response that changes its settlement fields.
    """

    @property
    def account(self) -> str:
        """Classic address authorized to submit the recipient claim."""
        ...

    async def sign_claim(
        self,
        transaction: PaymentChannelClaim,
    ) -> PaymentChannelClaim:
        """Return the same prepared claim with a valid XRPL signature."""
        ...


class LocalSeedRecipientSigner:
    """Local-wallet adapter for development and secret-injected deployments."""

    def __init__(self, seed: str) -> None:
        normalized = seed.strip()
        if not normalized:
            raise ValueError("recipient seed is required")
        self._wallet = Wallet.from_seed(normalized)

    @property
    def account(self) -> str:
        return self._wallet.address

    async def sign_claim(
        self,
        transaction: PaymentChannelClaim,
    ) -> PaymentChannelClaim:
        signed = await asyncio.to_thread(sign, transaction, self._wallet)
        if not isinstance(signed, PaymentChannelClaim):
            raise ValueError("recipient signer returned an unexpected transaction type")
        return signed
