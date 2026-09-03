# Demo variants

Use the XRP Testnet quickstart first. Issued currencies and Payment Channels add
ledger prerequisites that can obscure HTTP integration mistakes.

## One-time charge variants

| Variant | Currency wire value | Extra prerequisite |
| --- | --- | --- |
| XRP | `XRP` | Funded payer account |
| Issued currency | `{"currency":"...","issuer":"r..."}` | Payer trust line and balance; facilitator allowlist |
| MPT | `{"mpt_issuance_id":"..."}` | Holder balance; facilitator issuance-ID allowlist |

The JSON descriptor is itself the MPP `currency` string. Build it with the core
serializer instead of hand-ordering or whitespace-formatting JSON.

## PaymentChannel session

PaymentChannel sessions currently use XRP. A session test needs:

1. a payer wallet and public signing key accepted by the facilitator;
2. a signed `PaymentChannelCreate` transaction matching payer, recipient,
   amount, and network;
3. storage of the resulting channel ID;
4. signed cumulative claims that strictly increase;
5. optional recipient redemption and separate funder close handling.

The cumulative value is the total channel claim. If the last accepted claim is
`1000` drops and the next request costs `250`, the next claim is `1250`, not
`250`.

The channel can also be opened outside MPP and imported from validated ledger
state on its first voucher/close. Top up with the XRPL
`PaymentChannelFund` transaction, not an MPP action; a later voucher causes the
facilitator to verify and adopt the increased funding.

MPP `close` is a final voucher. With `PAYCHANNEL_RECIPIENT_SEED`, the facilitator
can redeem that voucher on-ledger; without it, the voucher remains off-ledger
for a separate redemption process. Recipient redemption does not set `tfClose`
or refund unused funding. The funder must close the XRPL channel separately.
For unattended operation, prefer an injected KMS/HSM `RecipientSigner`; the
local seed adapter can optionally run the bounded Redis-leased redemption and
idle-finalization worker.

## Pull versus push charge

- **Pull:** the credential contains the signed transaction blob and the
  facilitator submits it after exact validation.
- **Push:** the payer submits first and the credential contains the transaction
  hash; the facilitator resolves and validates it.

Use pull for the simplest controlled demo. Use push only when the payer needs
submission control and can wait for the transaction to be discoverable.

## MCP transport demo

Native MCP payment metadata is exercised with `xrpl-mpp-mcp`. It uses `_meta`
instead of HTTP headers. Keep this separate from an HTTP proxy demo so the
transport boundary is unambiguous.
