# Client

`xrpl-mpp-client` is the buyer-side Python SDK for MPP HTTP.

Main entrypoints:

- `XRPLPaymentSigner`
- `XRPLPaymentTransport`
- `wrap_httpx_with_mpp_payment(...)`
- `select_payment_challenge(...)`

It supports both `charge` and `session` HTTP intents for the repo-local `xrpl` payment method.
