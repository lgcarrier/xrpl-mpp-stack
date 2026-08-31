# Seller integration

Protect seller routes with `PaymentMiddlewareASGI` and a route configuration
built by `require_payment(...)` or `require_session(...)`.

## Charge routes

A charge route can advertise multiple XRPL offers. For each option define the
named network, recipient, amount, canonical currency, and optional description,
external ID, tags, or memos. The middleware:

1. returns fresh `WWW-Authenticate: Payment` challenges when no credential is
   present;
2. verifies the echoed challenge binding and body digest;
3. forwards the credential to the facilitator using seller bearer auth;
4. stores the verified receipt at `request.state.mpp_payment`;
5. forwards the original request to the application;
6. attaches `Payment-Receipt` only to a successful application response.

Choose `credentialHeader="Payment-Authorization"` for routes that also use
ordinary bearer authentication.

## PaymentChannel routes

`require_session(...)` configures an XRP PaymentChannel offer. The route emits
`open`, `voucher`, or `close` terms based on channel state and request hints.
The facilitator validates signatures and atomically advances the cumulative
high-water mark.

Treat `close` as a final voucher. It is not evidence that XRPL returned unused
funding or deleted the channel. Recipient-side redemption is available only
when the facilitator is explicitly configured with the recipient seed; the
funder remains responsible for the XRPL `tfClose`/refund lifecycle.

Do not recreate the 0.1 request-counter or server-issued balance credential on
top of this flow. The signed cumulative claim is the authorization evidence.

## Discovery

Use `augment_openapi_from_route_configs(...)` to add draft-01
`x-payment-info` offers to OpenAPI 3.x operations and optional
`x-service-info` metadata. Prefer the multi-offer form. Decimal values that
cannot be truthfully represented as integer base units are advertised as
dynamic (`amount: null`).

Discovery supports only `charge` and `session`. A runtime `402` remains
authoritative if the OpenAPI document is cached or pricing is dynamic.

## Hooks and relay

`HookDispatcher` can deliver typed challenge, credential, verification, and
settlement lifecycle events to application callbacks. Configure callback
timeouts and whether failures are best-effort or fatal. Event models exclude
raw payment evidence.

`PaymentOutcomeRelay` can send an allowlisted validated outcome to an HTTPS
application endpoint with an idempotency key. It rejects secret-bearing fields,
local/private endpoints by default, unbounded timeouts, and non-success HTTP
responses. It is optional application architecture and does not replace the
facilitator or MPP receipt.

## Production checklist

- Authenticate every facilitator payment endpoint.
- Use TLS and restrict facilitator ingress.
- Share Redis across replicas.
- Rotate challenge secrets safely.
- Require validated settlement before invoking the protected application.
- Keep body limits and rate limits enabled.
- Never expose FastAPI docs or detailed payment failures unintentionally.
