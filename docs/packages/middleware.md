# Middleware

`xrpl-mpp-middleware` protects seller routes with MPP HTTP payment challenges.

## Use It When

Use this package inside an ASGI or FastAPI seller app when you want selected
routes to return `402 Payment Required` until the buyer completes an XRPL-backed
MPP flow.

## Install

```bash
pip install xrpl-mpp-middleware
```

## Main APIs

Use:

- `require_payment(...)` for one-shot `charge`
- `require_session(...)` for prepaid `session`
- `PaymentMiddlewareASGI(...)` to wire challenges and facilitator validation into an ASGI app

Verified receipts are exposed as `request.state.mpp_payment`.

`WWW-Authenticate` and `Authorization` scheme matching is case-insensitive, though examples keep the canonical `Payment` casing.

Protected routes enforce a request body limit of `32768` bytes by default. Override it with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)`; oversized protected requests return `413 {"detail":"Request body too large"}` before facilitator or app processing.

## Minimal Charge Example

```python
from fastapi import FastAPI
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
            description="One premium request",
        )
    },
    challenge_secret="replace-with-a-shared-secret",
)
```

## Session Routes

Use `require_session(...)` when you want one XRPL prepayment to cover multiple
requests on the same protected route. The middleware:

- emits a `session` challenge with `sessionId`, `unitAmount`, and `minPrepayAmount`
- forwards `open`, `use`, `top_up`, and `close` actions to the facilitator
- exposes the verified receipt to the app just like a `charge` flow

The buyer reuses `X-MPP-Session-Id` to associate follow-up requests with the same
session.

## Route Configuration Notes

- Route keys use `"METHOD /path"` syntax such as `"GET /premium"`.
- Each protected route can accept `charge`, `session`, or both depending on the
  route config you choose.
- The middleware computes a request body digest and binds it into the challenge so
  the paid retry matches the exact protected request payload.
