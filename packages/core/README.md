# xrpl-mpp-core

Shared XRPL + MPP HTTP models and helpers.

## Install

```bash
pip install xrpl-mpp-core
```

## Includes

- MPP challenge, credential, receipt, and problem-detail models
- JCS + base64url codecs for `Authorization: Payment` and `Payment-Receipt`
- `WWW-Authenticate: Payment` parsing/rendering
- XRPL asset helpers for XRP, RLUSD, and USDC
- challenge binding, expiry, and body-digest helpers

## Example

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

Use `xrpl-mpp-core` directly when you need to generate or validate MPP HTTP headers without pulling in the facilitator, middleware, or client runtime packages.
