# XRPL MPP Stack

Hosted docs for the XRPL-first MPP HTTP stack.

## Packages

| Package | Install | Purpose |
| --- | --- | --- |
| `xrpl-mpp-core` | `pip install xrpl-mpp-core` | Shared MPP models and XRPL helpers |
| `xrpl-mpp-facilitator` | `pip install xrpl-mpp-facilitator` | FastAPI facilitator for `charge` and `session` |
| `xrpl-mpp-middleware` | `pip install xrpl-mpp-middleware` | ASGI middleware for protected seller routes |
| `xrpl-mpp-client` | `pip install xrpl-mpp-client` | Buyer-side HTTPX transport and signer |
| `xrpl-mpp-payer` | `pip install xrpl-mpp-payer` | CLI, proxy, and MCP payer runtime |

## Key docs

- [Header Contract](how-it-works/header-contract.md)
- [Payment Flow](how-it-works/payment-flow.md)
- [MPP HTTP Integration](integrations/mpp-http.md)
