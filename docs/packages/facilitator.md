# Facilitator

`xrpl-mpp-facilitator` is the settlement service behind the middleware.

## Use It When

Use this package when you are operating the seller-side service that validates
presigned XRPL transactions, enforces replay rules, and manages prepaid `session`
balances.

The facilitator stays non-custodial: it verifies and settles buyer-signed
transactions but does not hold the buyer's private key or run an internal ledger.

## Install

```bash
pip install xrpl-mpp-facilitator
```

## Run Locally

Set the core runtime variables first:

- `MY_DESTINATION_ADDRESS`
- `FACILITATOR_BEARER_TOKEN` when `GATEWAY_AUTH_MODE=single_token`
- `REDIS_URL`
- `MPP_CHALLENGE_SECRET`

Then start the service:

```bash
xrpl-mpp-facilitator --reload
```

For local docs and testing, the application loads `.env` automatically when present.

## Endpoints

It exposes:

- `GET /health`
- `GET /supported`
- `POST /charge`
- `POST /session`

`GET /supported` advertises the XRPL payment method and asset support the
middleware should expect. `POST /charge` handles exact-pay-per-request settlement.
`POST /session` handles `open`, `use`, `top_up`, and `close` session actions.

## Important Settings

The most important facilitator knobs are:

- `SETTLEMENT_MODE=validated` or `optimistic`
- `NETWORK_ID` and `XRPL_RPC_URL`
- `MAX_PAYMENT_LEDGER_WINDOW` for public `redis_gateways` mode
- `REPLAY_PROCESSED_TTL_SECONDS` for replay retention
- `SESSION_IDLE_TIMEOUT_SECONDS` and `SESSION_STATE_TTL_SECONDS` for prepaid sessions

The full environment reference is documented in [Configuration](../configuration.md).

## Python App Factory

```python
from xrpl_mpp_facilitator import create_app

app = create_app()
```

This is useful when you want to embed the facilitator in your own ASGI process
instead of launching the packaged CLI entry point.

## Operational Notes

- Redis is required for replay markers and session state.
- `validated` mode is the safer internet-facing default because the facilitator
  waits for XRPL validation before success.
- `redis_gateways` mode is stricter than `single_token` because it assumes
  third-party sellers or public relays may sit in front of the facilitator.
