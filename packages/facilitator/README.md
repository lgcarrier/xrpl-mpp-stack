# xrpl-mpp-facilitator

Authenticated FastAPI facilitator for Ripple-compatible XRPL MPP charge and
PaymentChannel verification.

```bash
pip install xrpl-mpp-facilitator
xrpl-mpp-facilitator --help
xrpl-mpp-facilitator --reload
```

Entry point: `xrpl_mpp_facilitator.main:app`

Factory: `xrpl_mpp_facilitator.factory:create_app`

Endpoints:

- `GET /health`
- `GET /supported`
- `POST /charge`
- `POST /session`

Charge credentials may contain a signed transaction blob or a submitted
transaction hash. The service enforces exact XRPL fields, named network,
source DID, invoice, freshness, successful settlement, and replay protection.
Session credentials use PaymentChannel `open`, cumulative `voucher`, and
`close` actions with atomic high-water state.

Configure gateway bearer authentication, Redis, XRPL RPC, destination,
challenge keys, allowlists, and validated settlement before starting. Never
expose payment endpoints without gateway authentication.
`XRPL_RPC_URL` requires HTTPS; local rippled development may explicitly set
`ALLOW_INSECURE_XRPL_RPC=true` only with a localhost/loopback HTTP URL.

`PAYCHANNEL_PAYER_PUBLIC_KEY` enables and restricts session verification. The
service can import a matching externally opened channel and notices a validated
out-of-band `PaymentChannelFund` increase before accepting a later voucher.
`PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS` rejects claims too close to an on-ledger
expiry/close deadline.

MPP `close` is a final voucher, not an on-ledger channel close. An optional
recipient signer lets the facilitator redeem that cumulative claim and wait for
validation; `PAYCHANNEL_RECIPIENT_SEED` is the built-in local adapter. Recipient
redemption does not set `tfClose`, refund unused XRP, or delete the channel.
Without a recipient signer, the MPP session is still finalized and its voucher
is retained for a separate redemption workflow.

Applications that construct `XRPLService` directly can inject a
`RecipientSigner` backed by a KMS, HSM, Vault, or remote signer instead of
placing a seed in the facilitator process. The complete claim is prepared by
the facilitator; signer responses that change any field or add `tfClose` are
rejected. `LocalSeedRecipientSigner` is the adapter used by
`PAYCHANNEL_RECIPIENT_SEED`.

Set `PAYCHANNEL_REDEEM_INTERVAL_SECONDS` above zero to enable the bounded
background redemption sweep. It uses a per-channel Redis lease across replicas,
processes at most `PAYCHANNEL_REDEEM_BATCH_SIZE` records per interval, and caps
recipient claim fees. `PAYCHANNEL_IDLE_CLOSE_SECONDS` additionally finalizes
inactive MPP sessions after redeeming their latest claim. “Idle close” is a
durable application-state transition only; the recipient transaction never
sets XRPL `tfClose`.

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/facilitator/>
