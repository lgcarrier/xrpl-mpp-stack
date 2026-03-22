# Deployment Modes

This page gives concrete environment examples for the most useful deployment
modes in this repo.

For the full variable reference, continue to [Configuration](../configuration.md).

## Mode Selector

| Mode | Best for | Auth mode | Network |
| --- | --- | --- | --- |
| Local Docker demo | First run, docs validation, Testnet walkthroughs | `single_token` | XRPL Testnet |
| Self-hosted seller + facilitator | One app team operating both sides | `single_token` | Testnet or Mainnet |
| Shared or public facilitator | Multi-seller or gateway-managed traffic | `redis_gateways` | Usually Testnet or Mainnet |
| Buyer workstation or agent | CLI, proxy, MCP, or automated buyers | n/a | Depends on target |

## Local Docker Demo

This is the simplest end-to-end mode and the one used by the quickstart:

```bash
GATEWAY_AUTH_MODE=single_token
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
NETWORK_ID=xrpl:1
XRPL_NETWORK=xrpl:1
SETTLEMENT_MODE=validated
MY_DESTINATION_ADDRESS=r...
FACILITATOR_BEARER_TOKEN=replace-with-a-random-secret
REDIS_URL=redis://127.0.0.1:6379/0
XRPL_WALLET_SEED=sEd...
MPP_CHALLENGE_SECRET=replace-with-a-long-random-secret
PRICE_DROPS=1000
PAYMENT_ASSET=XRP:native
```

Generate this automatically with:

```bash
python -m devtools.quickstart
```

Run it with:

```bash
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart --profile demo run --rm buyer
```

## Self-Hosted Seller + Facilitator

Use this when your seller app talks to one facilitator you operate yourself.

### Facilitator env

```bash
GATEWAY_AUTH_MODE=single_token
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
MY_DESTINATION_ADDRESS=rYourSettlementAddress
FACILITATOR_BEARER_TOKEN=replace-with-a-random-secret
REDIS_URL=redis://redis.internal:6379/0
NETWORK_ID=xrpl:1
SETTLEMENT_MODE=validated
MPP_CHALLENGE_SECRET=replace-with-a-long-random-secret
MPP_DEFAULT_REALM=merchant.example
```

### Seller app env

```bash
FACILITATOR_URL=https://facilitator.example
FACILITATOR_TOKEN=replace-with-a-random-secret
MERCHANT_XRPL_ADDRESS=rYourSettlementAddress
XRPL_NETWORK=xrpl:1
MPP_CHALLENGE_SECRET=replace-with-a-long-random-secret
MPP_DEFAULT_REALM=merchant.example
PRICE_DROPS=1000
```

This is the best default for one merchant app plus one facilitator service.

## Shared Or Public Facilitator

Use `redis_gateways` when you need per-gateway auth records instead of one shared token.

### Facilitator env

```bash
GATEWAY_AUTH_MODE=redis_gateways
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
MY_DESTINATION_ADDRESS=rYourSettlementAddress
REDIS_URL=redis://redis.internal:6379/0
NETWORK_ID=xrpl:1
SETTLEMENT_MODE=validated
MPP_CHALLENGE_SECRET=replace-with-a-long-random-secret
MAX_PAYMENT_LEDGER_WINDOW=20
```

In this mode the facilitator looks up gateway tokens in Redis at:

```text
facilitator:gateway_token:<sha256(token)>
```

Each token record must contain:

- `status=active`
- `gateway_id=<non-empty-id>`

Example bootstrap:

```bash
export TOKEN="replace-with-a-random-secret"
TOKEN_HASH=$(python - <<'PY'
import hashlib
import os
print(hashlib.sha256(os.environ["TOKEN"].encode()).hexdigest())
PY
)
redis-cli HSET "facilitator:gateway_token:${TOKEN_HASH}" status active gateway_id seller-a
```

This mode also requires bounded XRPL ledger freshness, so buyers must include a
valid `LastLedgerSequence`.

## Buyer Workstation Or Agent Runtime

This is the minimal env for `xrpl-mpp pay`, `xrpl-mpp proxy`, or `xrpl-mpp mcp`:

```bash
XRPL_WALLET_SEED=sEd...
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
XRPL_NETWORK=xrpl:1
PAYMENT_ASSET=XRP:native
XRPL_MPP_MAX_SPEND=0.01
```

Useful commands:

```bash
xrpl-mpp pay https://merchant.example/premium --dry-run
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp mcp
```

## Demo Asset Variants

The demo mode supports three main assets:

### XRP

```bash
PRICE_DROPS=1000
PRICE_ASSET_CODE=XRP
PRICE_ASSET_ISSUER=
PRICE_ASSET_AMOUNT=
PAYMENT_ASSET=XRP:native
```

### RLUSD

```bash
PRICE_ASSET_CODE=RLUSD
PRICE_ASSET_ISSUER=rIssuer
PRICE_ASSET_AMOUNT=1.25
PAYMENT_ASSET=RLUSD:rIssuer
```

### USDC

```bash
PRICE_ASSET_CODE=USDC
PRICE_ASSET_ISSUER=rIssuer
PRICE_ASSET_AMOUNT=2.50
PAYMENT_ASSET=USDC:rIssuer
```

For the exact helper commands, continue to [Run Demo Variants](../quickstart/demo-variants.md).
