# Open XRPL MPP Stack

Hosted docs for the XRPL-first MPP HTTP stack:

- `xrpl-mpp-core`
- `xrpl-mpp-facilitator`
- `xrpl-mpp-middleware`
- `xrpl-mpp-client`
- `xrpl-mpp-payer`

## Start Here

If you want to see a real payment succeed on XRPL Testnet, go straight to the
[Testnet XRP quickstart](quickstart/testnet-xrp.md).

That flow uses:

- `python -m devtools.quickstart` to generate reusable Testnet wallets and `.env.quickstart`
- `docker compose --env-file .env.quickstart up --build` to run the facilitator, merchant, and Redis
- `docker compose --env-file .env.quickstart --profile demo run --rm buyer` to trigger the paid request

The quickstart probes public XRPL Testnet RPC servers and writes the first healthy
endpoint into `.env.quickstart` as `XRPL_RPC_URL`. Override that selection with
`XRPL_TESTNET_RPC_URL` or `python -m devtools.quickstart --xrpl-rpc-url ...` when
you want to pin a specific provider.

## Guides By Goal

- Understand the stack layout: [Architecture Overview](architecture.md)
- Protect seller routes: [Seller Integration](integrations/seller.md)
- Build or operate a buyer: [Buyer Integration](integrations/buyer.md)
- Choose the right environment settings: [Deployment Modes](configuration/deployment-modes.md)
- Run XRP, RLUSD, or USDC demos: [Run Demo Variants](quickstart/demo-variants.md)

## Package Chooser

Pick the package for the role you are building. Most integrators start with
`xrpl-mpp-middleware` on the seller side or `xrpl-mpp-client` on the buyer side,
then add `xrpl-mpp-facilitator` as the settlement service.

| Package | PyPI | Install | Use when |
| --- | --- | --- | --- |
| [Core](packages/core.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-core?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-core/) | `pip install xrpl-mpp-core` | You need the shared MPP models, codecs, and XRPL asset helpers directly. |
| [Facilitator](packages/facilitator.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-facilitator?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-facilitator/) | `pip install xrpl-mpp-facilitator` | You are running the FastAPI settlement service behind protected seller routes. |
| [Middleware](packages/middleware.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-middleware?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-middleware/) | `pip install xrpl-mpp-middleware` | You are protecting ASGI or FastAPI routes that should return `402` until paid. |
| [Client](packages/client.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-client?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-client/) | `pip install xrpl-mpp-client` | You are building a buyer that signs XRPL payments and retries MPP challenges automatically. |
| [Payer](packages/payer.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-payer?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-payer/) | `pip install xrpl-mpp-payer` | You want a turnkey buyer CLI, local proxy, receipts, or MCP support for agents. |

If you want the shortest path to a working stack, read the
[middleware guide](packages/middleware.md), the [client guide](packages/client.md),
then run the [Testnet XRP quickstart](quickstart/testnet-xrp.md).

## Install Commands

```bash
pip install xrpl-mpp-core
pip install xrpl-mpp-facilitator
pip install xrpl-mpp-middleware
pip install xrpl-mpp-client
pip install xrpl-mpp-payer
```

Full AI agent support:

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp skill install
xrpl-mpp mcp
claude mcp add xrpl-mpp-payer -- xrpl-mpp mcp
```

## Comparison Table

| Package | Runs where | Main entry points | Depends on facilitator | Optional extras |
| --- | --- | --- | --- | --- |
| `xrpl-mpp-core` | Shared library code | `PaymentChallenge`, `PaymentCredential`, `PaymentReceipt`, header codecs, XRPL asset helpers | No | None |
| `xrpl-mpp-facilitator` | Seller infrastructure or service tier | `create_app(...)`, `xrpl_mpp_facilitator.main:app`, `xrpl-mpp-facilitator` | It is the facilitator | None |
| `xrpl-mpp-middleware` | Seller app | `PaymentMiddlewareASGI`, `require_payment(...)`, `require_session(...)`, `XRPLFacilitatorClient` | Yes | None |
| `xrpl-mpp-client` | Buyer app or integration test harness | `XRPLPaymentSigner`, `XRPLPaymentTransport`, `wrap_httpx_with_mpp_payment(...)` | Yes, against a protected seller route | None |
| `xrpl-mpp-payer` | Buyer operator or local agent runtime | `xrpl-mpp`, `pay_with_mpp(...)`, `XRPLPayer`, bundled skill, stdio MCP server | Yes, against a protected seller route | `[mcp]` |

## Beyond XRP

The primary quickstart is XRP on Testnet for the fastest real success path.

When you switch the demo to issued assets, the merchant uses `PRICE_*` variables
and the buyer uses `PAYMENT_ASSET`. The quickstart wallet cache keeps one shared
merchant wallet plus dedicated buyer wallets for XRP, RLUSD, and USDC, so the
derived env files can run in parallel without sharing one signing account.

### RLUSD Demo Config

Generate a derived env file:

```bash
python -m devtools.demo_env --asset rlusd
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

Use the [RLUSD guide](asset-guides/rlusd.md) for faucet setup, trustline details,
and claim recovery behavior.

### USDC Demo Config

Generate a derived env file:

```bash
python -m devtools.demo_env --asset usdc
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.usdc up --build
docker compose --env-file .env.quickstart.usdc --profile demo run --rm buyer
```

Use the [USDC guide](asset-guides/usdc.md) for the Circle faucet flow and sweep behavior.

## Configuration At A Glance

The full environment reference lives in [Configuration](configuration.md). For the
common local stack, the important knobs are:

- `MY_DESTINATION_ADDRESS` for the merchant settlement wallet
- `FACILITATOR_BEARER_TOKEN` for middleware-to-facilitator auth in `single_token` mode
- `REDIS_URL` for replay protection and session state
- `MPP_CHALLENGE_SECRET` for binding MPP challenges to the protected request
- `NETWORK_ID`, `XRPL_NETWORK`, and `XRPL_RPC_URL` for the XRPL network you are targeting
- `PRICE_*` and `PAYMENT_ASSET` for demo pricing and issued-asset examples

## What The Stack Does

This MPP-native stack exposes the current facilitator contract:

- `GET /health`
- `GET /supported`
- `POST /charge`
- `POST /session`

The middleware emits `WWW-Authenticate: Payment`, buyers retry with
`Authorization: Payment`, and successful responses include `Payment-Receipt`.

The request lifecycle is documented in [Payment Flow](how-it-works/payment-flow.md).
The exact wire format is documented in
[Header Contract](how-it-works/header-contract.md). Replay protection, bounded
ledger freshness, and session-state behavior are documented in
[Replay And Freshness](how-it-works/replay-and-freshness.md). Integration guidance
for MPP-native sellers and buyers lives in [Seller Integration](integrations/seller.md),
[Buyer Integration](integrations/buyer.md), and
[MPP HTTP Integration](integrations/mpp-http.md).
