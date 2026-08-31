# `xrpl-mpp-client`

Install:

```bash
pip install xrpl-mpp-client
```

The client package signs XRPL payment evidence and integrates it with `httpx`.

## Signer

`XRPLPaymentSigner` supports:

- charge pull credentials containing a signed Payment blob;
- charge push credentials containing a transaction hash;
- PaymentChannel open credentials;
- signed cumulative voucher credentials;
- close credentials;
- XRPL DID sources, deterministic InvoiceID binding, tags, and memos;
- recipient, amount, currency, and network policy before signing;
- HTTPS-only RPC by default, a final autofilled fee ceiling, and bounded IOU
  source/path policy.

An issued-currency payment without `XRPLIOUPathfindingPolicy` is direct-only
and sets `SendMax` equal to the exact destination `Amount`. To pay transfer
fees or use a cross-currency route, configure an explicit source currency,
absolute `max_source_amount`, and `slippage_bps` from 0 through 1000. The
signer never enables partial payment. It does not enumerate wallet holdings or
support MPT pathfinding.

## Transport

`XRPLPaymentTransport` sends `Accept-Payment`, parses multiple challenges,
selects a supported offer, preserves ordinary bearer auth, and retries once with
the selected credential header. HTTPS is required by default; plaintext
loopback development needs the explicit `allow_insecure_localhost=True` opt-in.
For successful charges, the receipt reference must equal the signed
transaction hash (or supplied push hash) before the response is accepted.

For Payment Channels, `register_open_transaction(...)` supplies a signed
`PaymentChannelCreate` blob and `register_channel(...)` resumes a known channel
with its cumulative high-water amount. `close_session(...)` requests an
explicit final proof; it does not submit the funder's XRPL `tfClose`
transaction, delete the channel, or refund unused XRP.

Open credentials derive the deterministic channel ID from the signed create
transaction, bind its payer, recipient, claim key, and funding, and sign that
real ID. A nonzero initial cumulative claim is allowed only up to the channel
funding amount.

The transport stores only client-side URL/channel coordination. It does not
receive or replay a server-issued session credential.

## Safety

Always set signer policy for production buyers. A syntactically valid challenge
is not automatically acceptable. The buyer remains responsible for checking
recipient, spend, currency, network, expiry, and trust context before signing.
