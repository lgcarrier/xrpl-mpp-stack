# Replay And Freshness

The facilitator protects exact-pay-per-request flows in two layers:

- Redis replay markers keyed by both the challenge reference and signed-blob hash
- XRPL ledger-window checks in public-gateway mode

## Replay Keys

Every XRPL-backed payment attempt resolves to two replay identifiers:

- the challenge reference carried in XRPL `InvoiceID`
- `blob_hash`

For MPP `charge`, the signed XRPL transaction must carry the challenge `invoiceId` in `InvoiceID`.

For MPP `session open` and `session top_up`, the signed XRPL transaction must carry the challenge `sessionId` in `InvoiceID`.

If the signed transaction omits that reference or uses a different value, the facilitator rejects the payment before settlement.

That means two requests collide if they reuse either:

- the same XRPL `InvoiceID`
- the same signed transaction blob

## Charge And Session Settlement

For `charge`, middleware forwards the decoded MPP credential to `POST /charge`, and the facilitator:

1. verifies the challenge binding and expiry
2. validates the signed XRPL payment and reserves both replay keys as `pending`
3. submits the transaction to XRPL
4. either converts the reservation to `processed` or releases it on failure
5. returns a `PaymentReceipt`

For `session`, the facilitator uses `POST /session`:

1. `open` and `top_up` reuse the same replay reservation and XRPL settlement path, keyed by `sessionId`
2. `use` and `close` mutate Redis-backed session state without submitting a new XRPL transaction

This keeps replay protection and settlement atomic while matching the current MPP HTTP API surface.

## Redis State

Replay markers for `charge`, `session open`, and `session top_up` are stored in Redis as:

- `pending:<reservation_id>` while a settlement is in progress
- `processed` after a successful settlement path

Defaults:

- processed TTL: `REPLAY_PROCESSED_TTL_SECONDS`, default `604800` seconds
- pending TTL: `max(VALIDATION_TIMEOUT + 60, 300)`

If either replay key already exists, the facilitator rejects the payment with:

```text
Transaction already processed (replay attack)
```

## Settlement Mode Effects

### `validated`

In `validated` mode, the facilitator:

1. submits the transaction
2. polls XRPL for up to `VALIDATION_TIMEOUT` seconds
3. waits for `tx.result.validated`
4. checks the delivered amount against the required exact amount
5. returns `status="validated"`

If validation never arrives in time, the pending reservation is released and settlement fails.

### `optimistic`

In `optimistic` mode, the facilitator:

1. submits the transaction
2. marks the replay reservation processed immediately
3. returns `status="submitted"`

This mode lowers latency, but it shifts more validation responsibility to the surrounding system.

## Freshness Rules In `redis_gateways` Mode

When `GATEWAY_AUTH_MODE=redis_gateways`, the facilitator additionally requires bounded XRPL timing:

- the signed transaction must include `LastLedgerSequence`
- `LastLedgerSequence` must be greater than the latest validated ledger
- `LastLedgerSequence` must not exceed `current_validated_ledger + MAX_PAYMENT_LEDGER_WINDOW`

If any of those checks fail, the facilitator rejects the payment before settlement.

This is why public-gateway mode is stricter than `single_token`: it assumes third-party sellers may retry or relay traffic, so the facilitator enforces a narrow ledger window for safer exact-payment handling.

## Practical Guidance

- generate a fresh signed transaction per paid request
- use the challenge `invoiceId` or `sessionId` as the XRPL `InvoiceID` when you sign manually
- leave XRPL autofill enabled, or set `LastLedgerSequence` yourself, when using `redis_gateways`
- prefer `validated` settlement for internet-facing deployments

For the on-the-wire request/response format, continue to [Header Contract](header-contract.md).
