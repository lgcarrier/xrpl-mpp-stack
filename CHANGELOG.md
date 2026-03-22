# Changelog

All notable changes to the Open XRPL MPP Stack are documented here.

## xrpl-mpp-core 0.1.3

- Accepted case-insensitive `Payment` auth schemes when parsing `WWW-Authenticate` and `Authorization` headers while keeping canonical header rendering as `Payment`.
- Aligned the package with the coordinated `0.1.3` stack release.

## xrpl-mpp-facilitator 0.1.3

- Aligned the package with the coordinated `0.1.3` stack release.
- Expanded the package README with install, runtime, and app-factory usage examples for clean-install and publish verification.

## xrpl-mpp-middleware 0.1.3

- Protected routes now enforce a configurable request body limit with a default of `32768` bytes and return `413 {"detail":"Request body too large"}` before facilitator or app processing.
- Unprotected routes now bypass facilitator startup, so facilitator outages do not block unrelated endpoints on first request.
- Expanded the package README with install, setup, and body-limit configuration guidance for the coordinated `0.1.3` stack release.

## xrpl-mpp-client 0.1.3

- Aligned the package with the coordinated `0.1.3` stack release.
- Expanded the package README with an async usage example for clean-install and publish verification.

## xrpl-mpp-payer 0.1.3

- Aligned the package with the coordinated `0.1.3` stack release.
- Expanded the package README with CLI, environment, and `mcp` extra usage guidance for clean-install and publish verification.

## xrpl-mpp-core 0.1.2

- Added `xrpl_currency_code(...)` as the shared helper for rendering XRPL issued-currency codes from 3-character, 20-byte ASCII, or 40-character hex asset identifiers.
- Relaxed issued-amount equality checks in exact-payment matching so numerically equivalent decimal strings compare correctly.

## xrpl-mpp-client 0.1.2

- Normalized issued-currency codes before building signed XRPL payments, so non-3-character asset codes serialize in XRPL wire format correctly.
- Accepted lowercase `www-authenticate` response headers in addition to the canonical casing when decoding `WWW-Authenticate: Payment` challenges.
- Raised the `xrpl-mpp-core` dependency floor to `0.1.2` to require the shared issued-currency encoding helper.

## xrpl-mpp-core 0.1.1

- Added the shared XRPL Testnet RPC resolver used by quickstart tooling and other Testnet-aware flows to find a healthy public JSON-RPC endpoint.

## xrpl-mpp-payer 0.1.2

- Added automatic public XRPL Testnet RPC selection when `XRPL_RPC_URL` is unset and `XRPL_NETWORK=xrpl:1`.
- Raised the `xrpl-mpp-core` dependency floor to `0.1.1` so clean installs include the shared Testnet RPC resolver.

## xrpl-mpp-core 0.1.0

- Initial public release of the shared XRPL/MPP wire models, header codecs, asset helpers, and exact-payment matching utilities.
- Publishes the canonical `PaymentChallenge`, `PaymentCredential`, and `PaymentReceipt` models used across the stack.

## xrpl-mpp-facilitator 0.1.0

- Initial public release of the FastAPI facilitator service with stable `GET /health`, `GET /supported`, `POST /charge`, and `POST /session` endpoints.
- Publishes the `create_app(...)` app factory, `xrpl_mpp_facilitator.main:app`, and `xrpl-mpp-facilitator` CLI.

## xrpl-mpp-middleware 0.1.0

- Initial public release of the seller-side ASGI middleware for exact XRPL MPP payments.
- Publishes `PaymentMiddlewareASGI`, `require_payment(...)`, and `XRPLFacilitatorClient`.

## xrpl-mpp-client 0.1.0

- Initial public release of the buyer-side SDK for decoding `402` challenges, signing XRPL payments, and retrying requests via `httpx`.
- Publishes `XRPLPaymentSigner`, `XRPLPaymentTransport`, and `wrap_httpx_with_mpp_payment(...)`.

## xrpl-mpp-payer 0.1.0

- Added official MCP server + CLI integration for buyer-side XRPL MPP payments.
- Publishes the `xrpl-mpp` CLI, bundled payer skill, local auto-pay proxy, receipt tracking, and stdio MCP tools for Claude Desktop and Cursor.

## xrpl-mpp-client 0.1.1

- Updated `xrpl-py` compatibility to `4.5.0` so downstream payer/MCP installs resolve cleanly.

## xrpl-mpp-payer 0.1.1

- Updated the client dependency floor to `xrpl-mpp-client>=0.1.1` so clean installs resolve the MCP-compatible XRPL dependency set.
