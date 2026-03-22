# Core

`xrpl-mpp-core` provides the shared MPP HTTP models and XRPL helpers used by the rest of the stack.

## Use It When

Use `xrpl-mpp-core` directly when you need to generate or validate MPP HTTP
headers without pulling in the facilitator, middleware, or client runtime packages.

It is the lowest-level package in the repo and is useful for custom seller
integrations, custom buyers, or protocol tooling.

## Install

```bash
pip install xrpl-mpp-core
```

## Main Exports

Use it when you need direct access to:

- `PaymentChallenge`
- `PaymentCredential`
- `PaymentReceipt`
- `build_payment_challenge(...)`
- `parse_payment_authorization_header(...)`
- `encode_payment_receipt(...)`

Additional exported helpers cover:

- `WWW-Authenticate: Payment` parsing and rendering
- base64url + JCS encoding and decoding for credentials and receipts
- XRPL asset parsing for `XRP:native`, RLUSD, and USDC
- body-digest and challenge-binding helpers for protected request validation

## Minimal Example

```python
from xrpl_mpp_core import (
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    build_payment_challenge,
    parse_payment_challenge,
    render_payment_challenge,
)

challenge = build_payment_challenge(
    secret="replace-with-a-shared-secret",
    realm="merchant.example",
    method="xrpl",
    intent="charge",
    request_model=XRPLChargeRequest(
        amount="1000",
        currency="XRP:native",
        recipient="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
        methodDetails=XRPLChargeMethodDetails(
            network="xrpl:1",
            invoiceId="A" * 64,
        ),
    ),
    expires_in_seconds=300,
)

header_value = render_payment_challenge(challenge)
decoded = parse_payment_challenge(header_value)
```

## Notes

- Currency identifiers use `CODE:issuer` for issued assets and `XRP:native` for XRP.
- Charge requests bind to `invoiceId`; session requests bind to `sessionId`.
- The rest of the stack builds on these models, so `xrpl-mpp-core` is the right
  package when you need protocol correctness without any HTTP or FastAPI runtime.
