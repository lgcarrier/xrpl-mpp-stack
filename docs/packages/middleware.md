# Middleware

`xrpl-mpp-middleware` protects seller routes with MPP HTTP payment challenges.

Use:

- `require_payment(...)` for one-shot `charge`
- `require_session(...)` for prepaid `session`
- `PaymentMiddlewareASGI(...)` to wire challenges and facilitator validation into an ASGI app

Verified receipts are exposed as `request.state.mpp_payment`.

`WWW-Authenticate` and `Authorization` scheme matching is case-insensitive, though examples keep the canonical `Payment` casing.

Protected routes enforce a request body limit of `32768` bytes by default. Override it with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)`; oversized protected requests return `413 {"detail":"Request body too large"}` before facilitator or app processing.
