# `xrpl-mpp-facilitator`

Install and run:

```bash
pip install xrpl-mpp-facilitator
xrpl-mpp-facilitator --help
xrpl-mpp-facilitator --reload
```

ASGI entry point: `xrpl_mpp_facilitator.main:app`

App factory: `xrpl_mpp_facilitator.factory:create_app`

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process and configured-network health |
| `GET /supported` | Supported XRPL intents and currencies |
| `POST /charge` | Verify/settle a one-time charge credential |
| `POST /session` | Verify/settle a PaymentChannel action |

Payment POST endpoints require seller gateway bearer authentication, are body
limited, and are rate limited. Public buyers should never call them directly.

## Charge verification

The service accepts pull-mode transaction blobs and push-mode transaction
hashes. It verifies the decoded ledger transaction against the challenge and
rejects wrong network, source, destination, currency, amount, invoice, tags,
memos, partial-payment flags, ledger result, freshness, and replay state.

## Payment Channels

The channel service validates `PaymentChannelCreate`, claim signatures,
challenge/source/network binding, and strict cumulative advancement. Redis
updates are atomic. Matching channels opened outside the MPP flow are imported
from the validated ledger on their first voucher/close. A validated
`PaymentChannelFund` increase is adopted before a later claim advances.

An injected `RecipientSigner` optionally enables recipient-side redemption of a
final cumulative claim; `PAYCHANNEL_RECIPIENT_SEED` is the built-in local
adapter and must derive `MY_DESTINATION_ADDRESS`. This submits and validates a
`PaymentChannelClaim` without `tfClose`; it pays the recipient but does not
delete the channel or refund unused XRP. Without a recipient signer, MPP
`close` durably finalizes the session and retains the final off-ledger voucher;
it is not reported as an on-ledger close. Configure
`PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS` so new
claims are refused with enough time remaining before `Expiration` or
`CancelAfter`.

## Settlement

`validated` is the only supported charge settlement mode. A pull transaction
must validate successfully after submission; a push hash must resolve to the
same successful validated transaction. The receipt's `settlementStatus`
extension records the validated state.

See [Configuration](../configuration.md) before deployment.
