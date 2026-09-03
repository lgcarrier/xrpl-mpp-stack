# Open XRPL MPP Stack 0.2

This project provides a Python 3.12 seller, facilitator, and buyer stack for
Machine Payments Protocol payments on the XRP Ledger.

Version 0.2 follows the current
[MPP drafts](https://github.com/tempoxyz/mpp-specs) and interoperates with the
XRPL method implemented by the
[Ripple reference SDK](https://github.com/ripple/xrpl-mpp-sdk). It is a clean
wire break from the repository's 0.1 session design.

## Choose a package

| Package | Use it for |
| --- | --- |
| `xrpl-mpp-core` | MPP HTTP wire models, codecs, challenge binding, and XRPL profile types |
| `xrpl-mpp-facilitator` | Verifying and settling XRPL credentials behind authenticated seller gateways |
| `xrpl-mpp-middleware` | Protecting ASGI routes, issuing challenges, discovery, hooks, and relay |
| `xrpl-mpp-client` | Signing charges and PayChannel claims and retrying with `httpx` |
| `xrpl-mpp-payer` | Operating a payer CLI, local proxy, receipt store, or agent-facing service |
| `xrpl-mpp-mcp` | Adding native MPP payment metadata to MCP/JSON-RPC operations |

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
