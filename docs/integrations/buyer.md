# Buyer Integration

This guide is for buyers that need to pay MPP-protected XRPL resources.

There are three main ways to integrate:

- use `xrpl-mpp-client` inside an application
- use `xrpl-mpp-payer` as an operator CLI or local proxy
- use `xrpl-mpp-payer[mcp]` for local agent tooling

## Application Buyer With HTTPX

The simplest application-level integration is `wrap_httpx_with_mpp_payment(...)`:

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
        base_url="https://merchant.example",
    ) as client:
        response = await client.get("/premium")
        print(response.status_code)
        print(response.text)


asyncio.run(main())
```

That transport automatically:

- detects `402 Payment Required`
- parses `WWW-Authenticate: Payment`
- signs a charge or session credential
- retries with `Authorization: Payment`
- stores session tokens and reuses them on later requests

## Session Behavior

For `session` routes, the client transport can:

1. open a session with a signed XRPL prepayment
2. reuse the returned `sessionToken`
3. top up automatically when balance is low
4. close a session explicitly with `await transport.close_session(...)`

That makes `xrpl-mpp-client` the best fit when payment should feel like HTTP
transport behavior instead of explicit business logic in every call site.

## Asset Selection

Use canonical asset identifiers:

- `XRP:native`
- `RLUSD:rIssuer`
- `USDC:rIssuer`

If a seller advertises multiple payment options, use `select_payment_challenge(...)`
to choose the right challenge by network and asset.

## CLI Buyer

For manual ops or shell workflows:

```bash
xrpl-mpp pay https://merchant.example/premium --amount 0.001 --asset XRP
xrpl-mpp pay https://merchant.example/premium --dry-run
xrpl-mpp receipts --limit 20
xrpl-mpp budget --asset XRP
```

`--dry-run` is useful when you want to inspect the challenge flow before letting
the buyer sign and retry automatically.

## Local Proxy

If you want an unmodified tool or browser to talk through an auto-paying proxy:

```bash
xrpl-mpp proxy https://merchant.example --port 8787
```

That starts a local forward proxy which:

- forwards the original method, path, query, headers, and body
- pays upstream MPP challenges when needed
- returns the upstream response back to the caller

## MCP And Agent Tooling

For local agent access:

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp skill install
xrpl-mpp mcp
```

Claude Desktop can register it directly:

```bash
claude mcp add xrpl-mpp-payer -- xrpl-mpp mcp
```

## Environment Variables

The most important buyer-side variables are:

- `XRPL_WALLET_SEED`
- `XRPL_RPC_URL`
- `XRPL_NETWORK`
- `PAYMENT_ASSET`
- `XRPL_MPP_MAX_SPEND`
- `XRPL_MPP_RECEIPTS_PATH`

`xrpl-mpp-payer` defaults its network to `xrpl:1` when not configured, which is
convenient for local Testnet demos but should be made explicit in production.

## Common Flows

### Demo Buyer

```bash
python -m examples.buyer_httpx
```

That script loads `.env` from the repo root when present and then pays the
target route from `TARGET_URL`.

### Issued-Asset Demo

```bash
python -m devtools.demo_env --asset rlusd
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

The same shape works for USDC with `--asset usdc`.

## Where To Go Next

- [Run Demo Variants](../quickstart/demo-variants.md)
- [Seller Integration](seller.md)
- [Configuration](../configuration.md)
