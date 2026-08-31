# xrpl-mpp-client

Buyer-side XRPL signer and one-retry `httpx` transport for MPP 0.2.

```bash
pip install xrpl-mpp-client
```

`XRPLPaymentSigner` builds one-time charge transaction/hash credentials and
PaymentChannel open/voucher/close credentials. It supports named networks,
canonical currencies, source DIDs, InvoiceID binding, tags, memos, and local
recipient/amount/currency policy. When `rpc_url` is omitted, its JSON-RPC
endpoint follows the selected `mainnet`, `testnet`, or `devnet` network.
Autofilled charge and channel-create transactions cap `LastLedgerSequence` to
the authenticated challenge expiry; an existing tighter ledger bound is kept.
RPC endpoints must use HTTPS. Plaintext RPC is accepted only for an exact
localhost or loopback address when `allow_insecure_rpc=True` is explicitly set.
The signer also refuses a final autofilled transaction fee above
`max_fee_drops` (1,000 drops by default), and explicit `fee`, `sequence`, and
`last_ledger_sequence` inputs remain authoritative through autofill.

Issued-currency payments remain direct and set `SendMax` equal to `Amount` by
default, so an RPC cannot silently choose a different source asset or increase
wallet spend. Transfer-fee headroom and cross-currency paths require an
explicit source-side policy:

```python
from xrpl_mpp_client import XRPLIOUPathfindingPolicy, XRPLPaymentSigner

signer = XRPLPaymentSigner(
    wallet,
    network="testnet",
    iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
        source_currency="XRP",
        max_source_amount="1005000",  # absolute XRP drops ceiling
        slippage_bps=50,              # 0.5%; allowed range is 0-1000
    ),
)
```

The signer gives `ripple_path_find` only that source currency, validates the
returned source amount and protocol path bounds, applies the bounded slippage,
and refuses any route above `max_source_amount`. `Amount` remains the exact
challenge amount and `tfPartialPayment` is never enabled. Automatic holdings
enumeration, MPT pathfinding, unbounded `SendMax`, and caller-supplied arbitrary
path sets are deliberately unsupported.

`XRPLPaymentTransport` sends `Accept-Payment`, selects from multiple challenges,
uses the exact credential header chosen by the server, preserves ordinary
bearer auth, requires HTTPS, and retries once. Plaintext loopback development
requires the explicit `allow_insecure_localhost=True` opt-in. Automatic
signing fails closed unless a complete `XRPLPaymentPolicy` is passed to the
transport or the signer was configured with all three constructor guardrails:
`expected_recipient`, `max_amount`, and `allowed_currencies`.
Before signing, the transport also buffers replayable request bytes and verifies
any challenge `digest` against the exact body that both attempts send. A paid
charge receipt is accepted only when its core reference is the exact hash of
the signed transaction (or the supplied push hash); optional XRPL receipt
extensions must also match the challenge and credential.

```python
from xrpl_mpp_client import XRPLPaymentPolicy, wrap_httpx_with_mpp_payment

policy = XRPLPaymentPolicy(
    expected_recipients="rMerchantAddress...",
    max_amount="10000",
    allowed_currencies=["XRP"],
    max_challenge_validity_seconds=300,
)
client = wrap_httpx_with_mpp_payment(signer, payment_policy=policy)
```

Known PaymentChannels are registered by channel ID and cumulative high-water
amount. Channel opening uses a caller-supplied signed `PaymentChannelCreate`
blob. The signer derives its real channel ID, binds its payer, recipient, claim
key, and funding, and supports a nonzero initial cumulative claim up to that
funding. A close credential is a final cumulative voucher; it does not submit the
funder's XRPL `tfClose` transaction or refund unused XRP. The 0.2 transport does
not store a server-issued session credential.

Direct signer methods remain available for interactive or externally approved
flows. Treat every challenge as untrusted until it passes local spend,
recipient, currency, network, and expiry policy. Automatic policy requires an
`expires` value and rejects a remaining validity window longer than its
configured limit (300 seconds by default).

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/client/>
