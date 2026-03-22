# Configuration

`.env.example` is the canonical starting point for local development, Docker
Compose demos, and operator handoff. The facilitator reads `.env` automatically
through Pydantic settings, and the local examples reuse the same variables.

## Common Local Testnet Profile

The quickstart generates `.env.quickstart` for you, but the equivalent values look like:

```bash
NETWORK_ID=xrpl:1
XRPL_NETWORK=xrpl:1
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
SETTLEMENT_MODE=validated
GATEWAY_AUTH_MODE=single_token
```

For local demos, keep `SETTLEMENT_MODE=validated` unless you are intentionally
testing lower-latency optimistic settlement behavior.

## Facilitator Runtime

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_AUTH_MODE` | `single_token` | Chooses either one shared bearer token or Redis-backed public gateway auth. |
| `XRPL_RPC_URL` | `https://s1.ripple.com:51234` | JSON-RPC endpoint used by the facilitator to inspect and submit XRPL transactions. |
| `MY_DESTINATION_ADDRESS` | required | XRPL address that receives settled payments. |
| `FACILITATOR_BEARER_TOKEN` | required in `single_token` mode | Shared secret that seller middleware uses to call the facilitator. |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis database for replay markers, gateway metadata, and session state. |
| `NETWORK_ID` | `xrpl:0` | CAIP-2 network id the facilitator advertises and validates against. |
| `SETTLEMENT_MODE` | `validated` | Wait for validation before success, or return early in `optimistic` mode. |
| `VALIDATION_TIMEOUT` | `15` | Maximum seconds to wait for on-ledger validation in `validated` mode. |
| `MIN_XRP_DROPS` | `1000` | Minimum XRP amount accepted for native-XRP payments. |
| `ALLOWED_ISSUED_ASSETS` | empty | Extra `CODE:ISSUER` pairs accepted beyond the built-in RLUSD and USDC issuers. |
| `ENABLE_API_DOCS` | `false` | Enables FastAPI docs UI. Keep this off for public deployments unless you explicitly want it. |

## MPP Challenge, Replay, And Session Controls

| Variable | Default | Purpose |
| --- | --- | --- |
| `MPP_CHALLENGE_SECRET` | required | Shared HMAC secret used to bind challenges to requests. |
| `MPP_DEFAULT_REALM` | unset | Optional override for the challenge `realm` shown to buyers. |
| `MPP_CHALLENGE_TTL_SECONDS` | `300` | Challenge expiry window used by middleware and facilitator. |
| `MAX_REQUEST_BODY_BYTES` | `32768` | Rejects oversized protected requests before facilitator or app handling. |
| `REPLAY_PROCESSED_TTL_SECONDS` | `604800` | TTL, in seconds, for processed replay markers. |
| `MAX_PAYMENT_LEDGER_WINDOW` | `20` | Maximum ledger window allowed in `redis_gateways` mode. |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `900` | Idle timeout for MPP `session` balances. |
| `SESSION_STATE_TTL_SECONDS` | `604800` | Redis TTL for persisted session-state records. |

## Demo And Docker Compose Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `FACILITATOR_PORT` | `8000` | Host port override for the facilitator service in Docker Compose. |
| `MERCHANT_PORT` | `8010` | Host port override for the merchant example service in Docker Compose. |
| `XRPL_WALLET_SEED` | empty | Buyer wallet seed used by the examples and real payer flows. |
| `PRICE_DROPS` | `1000` | XRP price for the merchant example when using native XRP. |
| `PRICE_ASSET_CODE` | `XRP` | Merchant example asset code. Set this to `RLUSD` or `USDC` for issued-asset demos. |
| `PRICE_ASSET_ISSUER` | empty | Issuer address for the merchant example when pricing in an issued asset. |
| `PRICE_ASSET_AMOUNT` | empty | Merchant example amount when pricing in an issued asset. |
| `PAYMENT_ASSET` | `XRP:native` | Buyer-side asset identifier used by the examples and payer. |

## Buyer And Payer Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `XRPL_NETWORK` | unset by the package, usually set in `.env` | Buyer-side CAIP-2 network id used for signing and challenge selection. |
| `XRPL_MPP_MAX_SPEND` | unset | Global spend cap used by `xrpl-mpp` budget checks when `--max-spend` is not passed. |
| `XRPL_MPP_RECEIPTS_PATH` | default local receipt store | Optional path override for payer receipt persistence. |

The payer resolves its network in this order:

1. explicit `network=` or CLI/runtime override
2. `XRPL_NETWORK`
3. `NETWORK_ID`
4. built-in fallback `xrpl:1`

That default makes the turnkey payer friendlier for Testnet demos, but in a
production deployment you should set `XRPL_NETWORK` and `XRPL_RPC_URL`
explicitly.

## Asset Configuration Examples

Native XRP demo:

```bash
PRICE_DROPS=1000
PAYMENT_ASSET=XRP:native
```

RLUSD demo:

```bash
PRICE_ASSET_CODE=RLUSD
PRICE_ASSET_ISSUER=rIssuerForRlusd
PRICE_ASSET_AMOUNT=0.001
PAYMENT_ASSET=RLUSD:rIssuerForRlusd
```

USDC demo:

```bash
PRICE_ASSET_CODE=USDC
PRICE_ASSET_ISSUER=rIssuerForUsdc
PRICE_ASSET_AMOUNT=0.001
PAYMENT_ASSET=USDC:rIssuerForUsdc
```

For the built-in Testnet and Mainnet RLUSD or USDC issuers, you can usually use
the repo helpers instead of hardcoding issuer values by hand:

- `python -m devtools.demo_env --asset rlusd`
- `python -m devtools.demo_env --asset usdc`

## Deployment Notes

- `FACILITATOR_BEARER_TOKEN` is required only when `GATEWAY_AUTH_MODE=single_token`.
- `REDIS_URL` is still required in both auth modes because replay and session state live in Redis.
- `ENABLE_API_DOCS=true` is best kept to local development or trusted internal environments.
- `MPP_CHALLENGE_SECRET` should be shared between the middleware that emits challenges and the facilitator that validates them.
- `redis_gateways` mode adds stricter `LastLedgerSequence` freshness checks. See [Replay And Freshness](how-it-works/replay-and-freshness.md) before enabling it on public traffic.

For concrete examples grouped by deployment shape, continue to
[Deployment Modes](configuration/deployment-modes.md).
