# Buyer integration

Use `xrpl-mpp-client` when payment is part of Python application code. Use
`xrpl-mpp-payer` when an operator needs a CLI, proxy, policy, receipt store, or
agent-facing service.

## Client responsibilities

1. Send `Accept-Payment` preferences.
2. Parse every Payment challenge on a `402` response.
3. Select a supported method/intent and validate all terms.
4. Enforce local recipient, amount, currency, and network policy.
5. Sign a charge or cumulative PaymentChannel proof.
6. Put the credential in the exact header selected by the challenge.
7. Retry the original request once.
8. Decode a receipt only from a successful response and bind its reference to
   the exact transaction or push hash that was authorized.

## Signer policy

Configure `XRPLPaymentSigner` with `network`, `expected_recipient`, `max_amount`,
and `allowed_currencies` wherever possible. Issued-currency and MPT values use
canonical JSON strings; legacy colon-delimited asset identifiers are not 0.2
wire values.

For a one-time charge, choose pull mode when the facilitator should submit the
signed blob. Choose hash mode only after the buyer has submitted the transaction
and can present its transaction hash.

IOU payments are direct-only by default, with source spend capped to the exact
destination amount. Transfer-fee headroom or cross-currency routing requires
`XRPLIOUPathfindingPolicy` with one explicit source asset, an absolute source
amount ceiling, and 0-1000 basis points of slippage. The RPC proposes paths but
does not choose the asset or spending limit. Partial payments, MPT pathfinding,
and automatic wallet-holdings enumeration are not supported.

## HTTP transport

`XRPLPaymentTransport` preserves ordinary bearer authorization when a challenge
selects `Payment-Authorization`, requires TLS by default, and performs at most
one automatic retry. Plaintext loopback development requires the explicit
`allow_insecure_localhost=True` opt-in.

For a known PaymentChannel, register the channel ID and current cumulative
amount for the protected URL. To create a channel through a session challenge,
register a signed `PaymentChannelCreate` transaction. Keep this mapping in
application state; it is not a server-issued session credential.
The open credential derives and signs the transaction's actual channel ID and
rejects mismatched payer, recipient, claim key, or funding. Its initial
cumulative claim may be nonzero but cannot exceed the channel funding.

The payer may instead create/fund the channel directly on XRPL. A later
voucher/close can trigger validated server-side import of a matching channel.
Use `PaymentChannelFund` out of band to increase its deposit; the next claim is
still cumulative and must not exceed the newly validated total funding.

`close_session(...)` signs a final voucher. It does not itself submit the
funder's `tfClose` transaction or guarantee return of unused XRP. Coordinate
the separate on-ledger close/refund lifecycle after the recipient has had time
to redeem its highest claim.

## Native MCP

Native paid MCP operations use `xrpl-mpp-mcp`, not HTTP headers. Credentials use
`org.paymentauth/credential` and receipts use `org.paymentauth/receipt` in
root-level JSON-RPC `_meta` or nested MCP `_meta` as required by the operation.

Supported paid operation names are `tools/call`, `resources/read`, and
`prompts/get`. Bind every challenge to the canonical operation request so a
credential for one tool/resource/prompt cannot authorize another.

## Security

- Never auto-pay a challenge outside configured spend and recipient policy.
- For the payer CLI/proxy/MCP wrapper, set the operator-approved destination
  with `--recipient` or `XRPL_MPP_EXPECTED_RECIPIENT`; a challenge-provided
  recipient is not sufficient authorization.
- Reject expired challenges before signing.
- Do not follow an untrusted challenge to a new RPC or facilitator endpoint.
- Protect the receipt store because receipts reveal transaction references.
- Use disposable Testnet wallets for demos and tests.
