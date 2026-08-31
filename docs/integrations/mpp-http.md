# MPP 0.2 compatibility and migration

## Current contracts

This repository targets the current upstream MPP HTTP auth, charge intent,
session-capable XRPL profile, discovery draft-01, and MCP transport contracts.
The MPP repository is the authority for generic protocol behavior; Ripple's
XRPL SDK is the interoperability reference for XRPL payloads and Payment
Channels.

## Clean-break changes from 0.1

| 0.1 form | 0.2 form |
| --- | --- |
| `xrpl:0`, `xrpl:1` | `mainnet`, `testnet` (`devnet` also supported) |
| `XRP:native` | `XRP` |
| `CODE:issuer` | JSON `{"currency":"CODE","issuer":"r..."}` string |
| one assumed payment header | default `Authorization` or selected `Payment-Authorization` |
| one challenge assumption | multiple challenges plus `Accept-Payment` ranking |
| non-canonical JSON encoding | RFC 8785 JCS plus unpadded base64url |
| reusable prepaid session credential | signed cumulative XRPL PaymentChannel claim |
| request-usage and refill actions | `open`, `voucher`, and `close` |
| receipt-specific required fields | four-field core plus method extensions |

Do not dual-accept legacy network, currency, or session wire values in a
production 0.2 endpoint. Regenerate persisted challenge/credential objects,
update both buyer and seller together, and clear or migrate state deliberately.

## Discovery is advisory

Discovery draft-01 publishes `x-service-info` and per-operation
`x-payment-info`. Its offer schema covers `charge` and `session`; it does not
cover the separate subscription intent draft. Clients must validate the fresh
runtime challenge regardless of discovery metadata.

## Subscription support

The core model permits future intent tokens, but an extensible parser is not a
settlement implementation. This repository does not advertise, sign, verify,
or settle XRPL subscriptions.

## MCP error mapping

Native MCP payment processing uses these exact JSON-RPC codes:

| Condition | Code |
| --- | ---: |
| payment required | `-32042` |
| payment verification failed | `-32043` |
| malformed credential metadata | `-32602` |
| internal payment processor failure | `-32603` |

Servers check root and MCP-nested `_meta`, reject conflicting placement, bind
the challenge to the paid operation, and include a receipt in successful result
metadata.
