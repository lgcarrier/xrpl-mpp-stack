# Open XRPL MPP Stack 0.2

This project provides a Python 3.12 seller, facilitator, and buyer stack for
Machine Payments Protocol payments on the XRP Ledger.

Version 0.2 follows the current
[MPP drafts](https://github.com/tempoxyz/mpp-specs) and interoperates with the
XRPL method implemented by the
[Ripple reference SDK](https://github.com/ripple/xrpl-mpp-sdk). It is a clean
wire break from the repository's 0.1 session design.

## Package Chooser

Pick the package for the role you are building. Most HTTP integrators start
with `xrpl-mpp-middleware` on the seller side or `xrpl-mpp-client` on the buyer
side, then add `xrpl-mpp-facilitator` as the verifier/settler service. For
native paid MCP operations, combine `xrpl-mpp-mcp` with the client or payer
package that fits your application.

These guides describe the 0.2 line. Each badge reports the version PyPI serves
right now; install only when it shows a compatible `0.2.x` release.

| Package | PyPI | Install | Use when |
| --- | --- | --- | --- |
| [Core](packages/core.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-core?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-core/) | `pip install xrpl-mpp-core` | You need the shared MPP HTTP models, codecs, challenge binding, or XRPL profile types directly. |
| [Facilitator](packages/facilitator.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-facilitator?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-facilitator/) | `pip install xrpl-mpp-facilitator` | You are running the authenticated verifier/settler service used by seller gateways. |
| [Middleware](packages/middleware.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-middleware?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-middleware/) | `pip install xrpl-mpp-middleware` | You are protecting ASGI or FastAPI routes, issuing challenges, or adding discovery, hooks, and relay. |
| [Client](packages/client.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-client?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-client/) | `pip install xrpl-mpp-client` | You are building a buyer that signs charge or PayChannel credentials and retries with `httpx`. |
| [Payer](packages/payer.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-payer?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-payer/) | `pip install xrpl-mpp-payer` | You want a turnkey buyer CLI, local proxy, receipts, spend policy, or optional agent server. |
| [MCP Transport](packages/mcp.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-mpp-mcp?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-mpp-mcp/) | `pip install xrpl-mpp-mcp` | You are adding framework-neutral MPP challenges, credentials, receipts, and errors to MCP/JSON-RPC operations. |

For the shortest HTTP path, read the [middleware guide](packages/middleware.md),
the [client guide](packages/client.md), then run the [Testnet XRP
quickstart](quickstart/testnet-xrp.md). For native MCP transport, start with the
[MCP package guide](packages/mcp.md) and add the [payer](packages/payer.md) when
you need an operator- or agent-facing client.

## What is implemented

- MPP HTTP `charge` and `session` challenges
- `Authorization: Payment` and challenge-selected `Payment-Authorization`
- `Accept-Payment` negotiation and multiple challenge selection
- successful-response `Payment-Receipt` fields and XRPL method extensions
- XRPL charge pull mode (signed blob) and push mode (transaction hash)
- XRP, issued currencies, and MPT currency descriptors
- named `mainnet`, `testnet`, and `devnet` networks
- PaymentChannel `open`, cumulative `voucher`, and `close` proofs
- discovery draft-01 OpenAPI metadata for `charge` and `session`
- native MCP transport metadata and paid-operation binding
- opt-in application lifecycle hooks and sanitized outcome relay

The generic upstream subscription intent is recognized as an extensibility
point, but this stack does not claim an XRPL subscription method profile.

## Start locally

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
pytest
```

Configure Testnet values in `.env`, then start the development services:

```bash
docker compose up --build redis facilitator merchant
```

Run the buyer from another terminal. Compose explicitly opts its merchant into
the private plaintext facilitator hop; the normal facilitator client requires
HTTPS and has no environment-only insecure override. Use an HTTPS facilitator
outside this development profile.

```bash
docker compose --profile demo run --rm buyer
```

Do not use a funded mainnet wallet for development. The live Testnet test is
excluded from normal `pytest` runs and must be enabled explicitly.

## Read next

- [Architecture](architecture.md)
- [Header contract](how-it-works/header-contract.md)
- [Payment flows](how-it-works/payment-flow.md)
- [Configuration](configuration.md)
- [0.1 migration notes](integrations/mpp-http.md)
