# Client

`xrpl-mpp-client` is the buyer-side Python SDK for MPP HTTP.

## Use It When

Use this package when you are building an application buyer or test harness that
should detect `402` responses, sign XRPL payments, and retry the request automatically.

## Install

```bash
pip install xrpl-mpp-client
```

## Main APIs

Main entrypoints:

- `XRPLPaymentSigner`
- `XRPLPaymentTransport`
- `wrap_httpx_with_mpp_payment(...)`
- `select_payment_challenge(...)`

It supports both `charge` and `session` HTTP intents for the repo-local `xrpl` payment method.

## Minimal Example

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

## Session Handling

`XRPLPaymentTransport` tracks active MPP sessions per request target. On a
session-protected route it will:

1. open the session with a signed XRPL payment
2. reuse the returned `sessionToken` on later retries
3. top up automatically when the facilitator asks for more prepaid balance
4. allow explicit teardown with `await transport.close_session(...)`

That makes it a good default transport for async HTTPX buyers that want to treat
MPP payment as a transport concern instead of hand-assembling credentials.

## Notes

- The signer copies the challenge `invoiceId` or `sessionId` into the XRPL `InvoiceID`.
- When `autofill_enabled=True`, the signer asks XRPL for fee and ledger bounds automatically.
- Use `select_payment_challenge(...)` directly when a seller offers multiple assets or networks and you want custom challenge selection logic.
