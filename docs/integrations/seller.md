# Seller Integration

This guide is for teams protecting seller routes with MPP while keeping XRPL
settlement in a separate facilitator service.

## What You Need

At minimum, a seller deployment needs:

- a FastAPI or Starlette app with `PaymentMiddlewareASGI`
- a facilitator reachable at `GET /supported`, `POST /charge`, and `POST /session`
- a shared challenge secret between middleware and facilitator
- a facilitator auth token or Redis-backed gateway auth setup

For a working reference implementation, see:

- `examples/merchant_fastapi/app.py`
- `docker-compose.yml`

## Minimal Charge Route

Use `require_payment(...)` when one request should map to one XRPL payment:

```python
from fastapi import FastAPI, Request
from xrpl_mpp_middleware import PaymentMiddlewareASGI, require_payment

app = FastAPI()

app.add_middleware(
    PaymentMiddlewareASGI,
    route_configs={
        "GET /premium": require_payment(
            facilitator_url="http://127.0.0.1:8000",
            bearer_token="replace-with-your-facilitator-token",
            pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            network="xrpl:1",
            xrp_drops=1000,
            description="One premium XRPL MPP request",
        )
    },
    challenge_secret="replace-with-a-long-random-secret",
    default_realm="merchant.example",
)


@app.get("/premium")
async def premium(request: Request) -> dict[str, str]:
    receipt = request.state.mpp_payment
    return {
        "message": "premium content unlocked",
        "payer": receipt.payer or "",
        "tx_hash": receipt.tx_hash or "",
    }
```

## Minimal Session Route

Use `require_session(...)` when one XRPL prepayment should cover several requests:

```python
from fastapi import FastAPI
from xrpl_mpp_middleware import PaymentMiddlewareASGI, require_session

app = FastAPI()

app.add_middleware(
    PaymentMiddlewareASGI,
    route_configs={
        "GET /metered": require_session(
            facilitator_url="http://127.0.0.1:8000",
            bearer_token="replace-with-your-facilitator-token",
            pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            network="xrpl:1",
            xrp_drops=250,
            min_prepay_amount="1000",
            idle_timeout_seconds=900,
            description="Metered session route",
        )
    },
    challenge_secret="replace-with-a-long-random-secret",
)
```

In this release, `unitAmount` must match `amount`, so variable metering is not
implemented yet.

## Required Inputs To Line Up

Whether you set values from environment variables or wire them directly in code,
these must agree across the seller app and facilitator:

- facilitator base URL
- facilitator bearer token or gateway auth mode
- XRPL network id such as `xrpl:1`
- seller destination XRPL address
- shared `MPP_CHALLENGE_SECRET`
- accepted asset identifiers and price amounts

If any of these drift, the middleware will usually fail at startup or the paid
retry will return another `402`.

## Facilitator Runtime

Start the facilitator locally with:

```bash
xrpl-mpp-facilitator --reload
```

Important facilitator settings:

- `MY_DESTINATION_ADDRESS`
- `FACILITATOR_BEARER_TOKEN` in `single_token` mode
- `REDIS_URL`
- `NETWORK_ID`
- `XRPL_RPC_URL`
- `MPP_CHALLENGE_SECRET`

The full matrix is documented in [Configuration](../configuration.md) and
[Deployment Modes](../configuration/deployment-modes.md).

## Route Behavior

When a protected request arrives, the middleware:

1. computes a request-body digest
2. emits one or more `WWW-Authenticate: Payment` challenges on `402`
3. validates `Authorization: Payment` via the facilitator
4. injects `request.state.mpp_payment`
5. adds `Payment-Receipt` to the successful response

Protected routes reject request bodies over `32768` bytes by default. Override
that with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)` if needed.

## Issued Assets

For issued assets such as RLUSD and USDC:

- use `amount=...` instead of `xrp_drops=...`
- set `asset_code` to the issuer currency code
- set `asset_issuer` to the issuer address

Example:

```python
require_payment(
    facilitator_url="http://127.0.0.1:8000",
    bearer_token="replace-with-your-facilitator-token",
    pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
    network="xrpl:1",
    amount="1.25",
    asset_code="RLUSD",
    asset_issuer="rIssuer",
)
```

## Local Verification

Run the example merchant app:

```bash
uvicorn examples.merchant_fastapi.app:app --reload --port 8010
```

Dry-run the buyer first:

```bash
xrpl-mpp pay http://127.0.0.1:8010/premium --amount 0.001 --asset XRP --dry-run
```

Then do a real paid request with the demo stack or a configured buyer:

```bash
python -m examples.buyer_httpx
```

## Where To Go Next

- [Architecture Overview](../architecture.md)
- [Buyer Integration](buyer.md)
- [Troubleshooting](../troubleshooting.md)
