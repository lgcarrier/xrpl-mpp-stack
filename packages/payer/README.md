# xrpl-mpp-payer

Operator-facing CLI, proxy, receipt store, spend policy, and optional agent
service built on `xrpl-mpp-client`.

```bash
pip install xrpl-mpp-payer
xrpl-mpp --help
```

For the optional payer agent server:

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp mcp
```

Typical commands:

```bash
export XRPL_MPP_EXPECTED_RECIPIENT=rYourApprovedMerchantAddress
xrpl-mpp pay https://merchant.example/premium --dry-run
xrpl-mpp pay https://merchant.example/metered --intent session --amount 1 --channel-funding-amount 1000000
xrpl-mpp pay https://merchant.example/metered --intent session --channel-id AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA --cumulative-amount 25000
xrpl-mpp close https://merchant.example/metered --channel-id AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA --cumulative-amount 50000
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp receipts
xrpl-mpp budget
```

Configure wallet material through environment variables or a secret manager and
apply strict network, recipient, currency, and spend policy. Do not give an
agent an unlimited funded wallet or rely on model-generated arguments as the
only policy boundary.

MPP 0.2 uses named XRPL networks (`mainnet`, `testnet`, `devnet`) and
currencies encoded as `XRP` or a canonical JSON descriptor. Session amounts
are incremental drops and PayChannel vouchers are cumulative. The removed
prepaid session token, `use`, and `top_up` shapes are not accepted.

Automatic signing fails closed unless the merchant recipient is explicitly
approved with `--recipient` or `XRPL_MPP_EXPECTED_RECIPIENT`. The payer binds
that recipient together with the selected currency and resolved spend cap in
an `XRPLPaymentPolicy`; challenges must expire and may be valid for at most 300
seconds. A registered channel retains its approved recipient for close only.

Buyer RPC endpoints require HTTPS. `ALLOW_INSECURE_XRPL_RPC=true` permits only
an exact localhost or loopback HTTP endpoint for development.
`XRPL_MPP_MAX_FEE_DROPS` caps the final autofilled transaction fee (default
1,000 drops). Optional IOU pathfinding requires both
`XRPL_MPP_IOU_SOURCE_CURRENCY` and `XRPL_MPP_IOU_MAX_SOURCE_AMOUNT`; the latter
is an absolute source-side ceiling, in XRP drops or issued-currency units.
`XRPL_MPP_IOU_SLIPPAGE_BPS` defaults to 50 and is bounded to 0-1000.

Successful charge receipts are recorded only after the receipt reference is
cryptographically bound to the exact transaction or push hash that the payer
authorized. A forged success receipt raises and does not create a local audit
record.

The `close` command signs and sends a final cumulative voucher. It does not
submit the funder's XRPL `tfClose` transaction, delete the channel, or refund
unused XRP. Those remain a separate on-ledger funder responsibility after the
recipient has had an opportunity to redeem its highest claim.

The optional payer agent server exposes controlled buyer behavior. For native
MPP payment metadata inside an MCP/JSON-RPC server, use the separate
`xrpl-mpp-mcp` package.

The agent tools intentionally do not accept a recipient or spend-cap override
from model-generated arguments. Non-dry-run `pay_url`, `close_channel`, and
`proxy_mode` load both `XRPL_MPP_EXPECTED_RECIPIENT` and
`XRPL_MPP_MAX_SPEND` from operator-controlled process configuration. The
environment cap remains a ceiling even when lower request-specific limits are
used elsewhere.

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/payer/>
