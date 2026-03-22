# xrpl-mpp-client

Buyer-side SDK for XRPL-backed MPP HTTP retries.

## Install

```bash
pip install xrpl-mpp-client
```

## Main APIs

- `XRPLPaymentSigner`
- `XRPLPaymentTransport`
- `wrap_httpx_with_mpp_payment(...)`
- `select_payment_challenge(...)`
- `build_payment_authorization(...)`

## Example

```python
import asyncio

from xrpl.wallet import Wallet
from xrpl_mpp_client import XRPLPaymentSigner, wrap_httpx_with_mpp_payment


async def main() -> None:
    signer = XRPLPaymentSigner(
        Wallet.from_seed("sEd..."),
        rpc_url="https://s.altnet.rippletest.net:51234/",
        network="xrpl:1",
    )
    async with wrap_httpx_with_mpp_payment(
        signer,
        asset="XRP:native",
    ) as client:
        response = await client.get("https://merchant.example/premium")
        print(response.status_code)
        print(response.text)


asyncio.run(main())
```

The client understands both `charge` and `session` challenges and reads/writes the MPP HTTP headers directly.
For explicit session teardown, reuse the same `XRPLPaymentTransport` instance and call `await transport.close_session("https://merchant.example/path")`.
