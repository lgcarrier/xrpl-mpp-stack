from __future__ import annotations


class SettlementPendingError(RuntimeError):
    """Ledger submission may have succeeded, but settlement is not yet known."""

    def __init__(self, tx_hash: str) -> None:
        self.tx_hash = tx_hash
        super().__init__(
            f"Payment {tx_hash} was submitted but has not reached validated settlement"
        )
