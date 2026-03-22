# xrpl-mpp-middleware

ASGI middleware for XRPL-backed MPP HTTP payments.

## Install

```bash
pip install xrpl-mpp-middleware
```

## Main APIs

- `PaymentMiddlewareASGI`
- `require_payment(...)`
- `require_session(...)`
- `XRPLFacilitatorClient`

The middleware emits MPP challenges, validates paid retries through the facilitator, injects `request.state.mpp_payment`, and adds `Payment-Receipt` on success.

## Example

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

`WWW-Authenticate` and `Authorization` scheme matching is case-insensitive, though examples keep the canonical `Payment` casing.

Protected routes enforce a request body limit of `32768` bytes by default. Override it with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)`; oversized protected requests return `413 {"detail":"Request body too large"}` before facilitator or app processing.
