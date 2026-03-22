# MPP HTTP Integration

This stack now integrates directly with the MPP HTTP contract instead of the legacy x402 adapters.

## Seller side

Use `PaymentMiddlewareASGI` with one or more `require_payment(...)` or `require_session(...)` route configs.

Header scheme matching is case-insensitive for both `WWW-Authenticate` and `Authorization`, though examples use canonical `Payment` casing.

Protected routes enforce a request body limit of `32768` bytes by default. Override it with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)`; requests above that ceiling receive `413 {"detail":"Request body too large"}` before facilitator or app processing.

## Buyer side

Use `XRPLPaymentSigner` together with `wrap_httpx_with_mpp_payment(...)` or the `xrpl-mpp` CLI.

## Facilitator side

Run `xrpl-mpp-facilitator` and point middleware routes at:

- `GET /supported`
- `POST /charge`
- `POST /session`

The facilitator validates XRPL-signed transactions and manages Redis-backed session state for the `session` intent.

For end-to-end implementation guidance, continue to:

- [Seller Integration](seller.md)
- [Buyer Integration](buyer.md)
- [Architecture Overview](../architecture.md)
