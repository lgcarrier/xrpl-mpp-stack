# Troubleshooting

## No Payment challenge on `402`

An MPP payment response must include at least one
`WWW-Authenticate: Payment ...` challenge. Check route matching, reverse-proxy
header forwarding, and middleware order. Do not synthesize terms from the
problem body.

## Credential replaced application bearer auth

Configure the route to issue `header="Payment-Authorization"`. The buyer should
preserve `Authorization: Bearer ...` and put the Payment credential in
`Payment-Authorization`. The credential's echoed challenge must contain the
same header selection.

## Network or currency rejected

0.2 accepts named networks: `mainnet`, `testnet`, and `devnet`. It accepts `XRP`
or canonical XRPL JSON currency descriptors. Legacy CAIP-like network strings,
`XRP:native`, and colon-delimited issued assets are intentionally rejected.

## Invoice mismatch

Copy the challenge-provided `invoiceId`, or use the shared deterministic
InvoiceID derivation from the challenge ID. A visually similar external ID or
session identifier is not interchangeable with the 64-hex XRPL InvoiceID.

## Transaction rejected after submission

Inspect safe decoded fields and the XRPL result:

- account/source DID;
- destination and destination tag;
- currency, issuer/MPT ID, and exact amount;
- InvoiceID, source tag, and requested memos;
- partial-payment flags;
- validated ledger result and ledger freshness;
- replay reservation state.

Do not print the wallet seed or full signed blob while debugging.

## Settlement status unknown

A `503` `settlement-pending` or `settlement-unknown` problem is not a payment
rejection. The transaction may already have validated even if the submit or
polling response was lost. Preserve its `paymentReference`, check that exact
transaction hash against a validated ledger, and retry the same credential only
when needed. Do not answer this state with a fresh charge or a newly signed
`PaymentChannelCreate`; that can pay or lock funds twice. These responses are
`private, no-store` and never carry a fresh challenge or `Payment-Receipt`.

## PaymentChannel voucher rejected

Confirm that the channel ID, named network, payer, recipient, and signing key
match the stored channel. The voucher amount must be the new cumulative total
and must be strictly greater than the accepted high-water value. All replicas
must share the same Redis channel state.

If the channel was opened or funded out of band, confirm its validated ledger
entry matches `PAYCHANNEL_PAYER_PUBLIC_KEY`, `MY_DESTINATION_ADDRESS`, minimum
settle delay, and closing margin. A `PaymentChannelFund` is visible only after
validation. New claims are intentionally refused within
`PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS` of `Expiration` or `CancelAfter`.

## PaymentChannel close did not close or refund the channel

MPP `close` is a final voucher. Without `PAYCHANNEL_RECIPIENT_SEED`, no
recipient-side ledger transaction is submitted. With it, the facilitator
submits and validates a claim for the final cumulative amount, but that claim
does not use `tfClose`, delete the channel, or refund unused XRP. The channel
funder must initiate XRPL closure, or the configured `CancelAfter` must elapse.

If background redemption is enabled, inspect the structured
`paychannel_redemption_*` events, confirm every replica shares Redis, and verify
the configured fee cap and signer availability. An ambiguous submit holds the
per-channel lease until it expires; do not manually issue a fresh, higher claim
while reconciling the existing cumulative amount.

## No receipt

`Payment-Receipt` belongs only on successful paid responses. If verification
failed or the protected application returned an error, its absence is correct.
Also check whether a reverse proxy strips the header.

## MCP error

- `-32042`: payment required; select a challenge.
- `-32043`: verification failed; use the fresh returned challenge.
- `-32602`: malformed credential metadata or conflicting `_meta` placement.
- `-32603`: internal payment processor failure.

Bind retries to the same paid operation request. A credential from an HTTP
exchange or another tool call is not transferable.

## Public Testnet instability

Faucets and public RPC endpoints can be unavailable or lagging. Confirm endpoint
health and the current ledger independently. Keep the live Testnet test opt-in;
the deterministic suite should still pass without network access.

## RLUSD funding is pending

Pin `XRPL_TESTNET_RPC_URL` when public Testnet endpoints are unhealthy. For a
wallet created with `--new-wallet`, rerun `python -m devtools.rlusd_fund` with
the standalone wallet file printed by the first invocation:

```bash
python -m devtools.rlusd_fund \
  --wallet-file .live-test-wallets/rlusd-funded-wallet-YYYYMMDDTHHMMSSZ.json \
  --target-rlusd 10 \
  --max-xrp 35
```

For the cached demo buyer, rerun the original command without `--wallet-file`;
its multi-wallet cache is not accepted as a standalone wallet file. The command
reconciles every journaled transaction before signing another. Exit status `3`
means the XRP faucet or ledger transaction is still pending, so preserve both
the private wallet and funding-state files. Exit status `2` is a bounded
liquidity or spend-cap refusal, while status `1` indicates a validation or
infrastructure failure.
