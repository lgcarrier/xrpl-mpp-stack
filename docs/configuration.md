# Configuration

Copy `.env.example` to `.env` and replace every placeholder. The exact template
in the repository is authoritative for the current checkout.

## Facilitator

| Variable | Purpose |
| --- | --- |
| `GATEWAY_AUTH_MODE` | `single_token` or Redis-backed gateway authentication |
| `FACILITATOR_BEARER_TOKEN` | Seller-to-facilitator bearer token in single-token mode |
| `REDIS_URL` | Replay reservations, rate-limit state, and PaymentChannel high-water records |
| `XRPL_RPC_URL` | Trusted XRPL JSON-RPC endpoint; HTTPS is required by default |
| `ALLOW_INSECURE_XRPL_RPC` | Development-only opt-in for an HTTP endpoint on localhost or a loopback IP |
| `NETWORK_ID` | `mainnet`, `testnet`, or `devnet` |
| `MY_DESTINATION_ADDRESS` | Required payment recipient |
| `SETTLEMENT_MODE` | Must be `validated`; other values are rejected |
| `VALIDATION_TIMEOUT` | Ledger validation polling timeout |
| `MIN_XRP_DROPS` | Minimum accepted XRP charge |
| `ALLOWED_ISSUED_ASSETS` | Explicit issued-currency allowlist |
| `ALLOWED_MPT_ISSUANCE_IDS` | Explicit MPT issuance-ID allowlist |
| `MAX_PAYMENT_LEDGER_WINDOW` | Accepted transaction freshness window |
| `REPLAY_PROCESSED_TTL_SECONDS` | Completed replay-marker retention |
| `MAX_REQUEST_BODY_BYTES` | Facilitator payment-endpoint body limit |
| `ENABLE_API_DOCS` | Expose FastAPI docs only when operationally appropriate |

`ALLOW_INSECURE_XRPL_RPC=true` never permits plaintext RPC to a remote host; it
only admits `http://localhost` or a loopback IP for a locally operated rippled.
Use HTTPS for Testnet, Devnet, Mainnet, containers, and remote/internal network
hosts.

## Challenge binding

| Variable | Purpose |
| --- | --- |
| `MPP_CHALLENGE_SECRET` | Active HMAC key for newly issued challenges |
| `MPP_CHALLENGE_PREVIOUS_SECRETS` | Comma-separated verification-only rotation keys |
| `MPP_CHALLENGE_TTL_SECONDS` | Challenge lifetime |
| `MPP_DEFAULT_REALM` | Optional middleware realm override |

Deploy the active secret to issuers and verifiers together. During rotation,
issue with the first key and verify with the active plus previous keys. Remove
old keys only after every challenge they signed has expired.

## Payment Channels

| Variable | Purpose |
| --- | --- |
| `PAYCHANNEL_PAYER_PUBLIC_KEY` | Required funder claim-key allowlist for enabling `/session` |
| `PAYCHANNEL_RECIPIENT_SEED` | Optional recipient wallet seed for validated server-side claim redemption |
| `PAYCHANNEL_MIN_SETTLE_DELAY` | Minimum acceptable on-ledger `SettleDelay`, in seconds |
| `PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS` | Refuse claims this close to `Expiration` or `CancelAfter` |
| `PAYCHANNEL_MAX_REDEMPTION_FEE_DROPS` | Maximum unattended recipient-claim fee, in drops |
| `PAYCHANNEL_REDEEM_INTERVAL_SECONDS` | Background redemption interval; `0` disables the worker |
| `PAYCHANNEL_IDLE_CLOSE_SECONDS` | Finalize inactive MPP sessions after this age; `0` disables idle finalization |
| `PAYCHANNEL_REDEEM_BATCH_SIZE` | Maximum channel records inspected per worker interval (maximum `1000`) |
| `PAYCHANNEL_REDEEM_LEASE_SECONDS` | Per-channel Redis lease; effective minimum is `VALIDATION_TIMEOUT + 60` |

Session routes currently support XRP channels. The open transaction must match
the network, payer, recipient, public key, funding, settle-delay, and expiry
policies; cumulative claims must strictly advance. If the seed is configured,
it must derive `MY_DESTINATION_ADDRESS`. Keep it in a secret manager, never in
source control or logs.

For KMS/HSM deployments, construct `XRPLService` with an injected
`RecipientSigner` instead of setting `PAYCHANNEL_RECIPIENT_SEED`. The signer
receives a fully prepared `PaymentChannelClaim` and returns its signed form;
the facilitator verifies that no transaction field changed. The seed and
injected signer modes are mutually exclusive.

MPP `close` is a final voucher. With a recipient signer, the facilitator submits
a recipient-signed `PaymentChannelClaim`, waits for validated success, and marks
the durable record redeemed/finalized. That transaction does not set `tfClose`,
refund unused XRP, or delete the channel. Without a recipient signer, the MPP
session is durably finalized and the final voucher is retained off-ledger for a
separate redemption workflow; no on-ledger close is claimed.

The optional background worker proactively redeems outstanding cumulative
claims. If `PAYCHANNEL_IDLE_CLOSE_SECONDS` is configured, it also finalizes an
inactive MPP session after successful redemption (or immediately when the
stored cumulative is already redeemed). Multiple replicas coordinate with a
Redis lease. Ambiguous failures keep that lease until expiry before retrying.
Neither explicit nor background recipient redemption sets `tfClose`; the funder
retains control of the XRPL channel-close/refund lifecycle.

`PaymentChannelFund` is not an MPP action. Submit it directly through XRPL, then
send a later voucher; validated ledger verification refreshes the increased
funding before advancing the high-water mark. A channel opened outside the MPP
flow can likewise be imported on its first voucher/close when its validated
ledger parties, key, funding, settle delay, and expiry satisfy policy.

There are no 0.2 settings for an application session timeout, reusable session
credential, request counter, or stored prepaid balance.

## Buyer policy

| Variable | Purpose |
| --- | --- |
| `XRPL_MPP_EXPECTED_RECIPIENT` | Operator-approved recipient used by the payer CLI/proxy/MCP server before automatic signing |
| `XRPL_MPP_MAX_SPEND` | Optional payer CLI/proxy ceiling in user asset units; required with the recipient for non-dry-run MCP tools |
| `XRPL_MPP_RECEIPTS_PATH` | Optional local payer receipt-store path |
| `XRPL_MPP_MAX_FEE_DROPS` | Maximum final autofilled XRPL transaction fee in drops; default `1000` |
| `XRPL_MPP_IOU_SOURCE_CURRENCY` | Explicit XRP or issued source asset authorized for IOU pathfinding |
| `XRPL_MPP_IOU_MAX_SOURCE_AMOUNT` | Absolute source-side spend ceiling paired with the source currency |
| `XRPL_MPP_IOU_SLIPPAGE_BPS` | Source quote buffer from `0` to `1000` basis points; default `50` |

Buyer code should configure the signer with an expected recipient, maximum
amount, allowed currencies, network, and trusted RPC endpoint. The payer fails
closed unless `--recipient` or `XRPL_MPP_EXPECTED_RECIPIENT` supplies the
operator-approved destination. The transport requires HTTPS except for
loopback development with the explicit `allow_insecure_localhost=True` opt-in,
and sends at most one paid retry.

The payer MCP tools load their recipient and ceiling only from process
configuration; model-generated tool arguments cannot replace either value.
Client examples convert an XRP `XRPL_MPP_MAX_SPEND` value to drops before
constructing the lower-level wire-amount policy. Their local `PRICE_AMOUNT`
fallback is already expressed in MPP wire units.

Never log or commit wallet seeds, signed blobs, payment credentials, facilitator
bearer tokens, or HMAC secrets.
