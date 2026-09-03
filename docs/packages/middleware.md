# `xrpl-mpp-middleware`

Install:

```bash
pip install xrpl-mpp-middleware
```

The package protects ASGI routes and connects a seller application to an
authenticated facilitator.

## Route protection

- `PaymentMiddlewareASGI`
- `require_payment(...)` for one-time charge offers
- `require_session(...)` for XRP PaymentChannel offers
- `ChargeRouteSpec`, `SessionRouteSpec`, and `RouteConfig`
- request-body limits and exact request-digest binding
- multiple challenges and `Accept-Payment` negotiation
- ordinary bearer-auth preservation with `Payment-Authorization`

After verification, the receipt is available as `request.state.mpp_payment`.
Middleware adds `Payment-Receipt` only to a successful downstream response.

## Discovery

`xrpl_mpp_middleware.discovery` creates draft-01 OpenAPI `x-payment-info` and
`x-service-info` extensions from route configuration. It supports multi-offer
`charge` and `session` metadata and marks non-integer pricing as dynamic.

## Hooks

`xrpl_mpp_middleware.hooks` provides immutable typed lifecycle events and a
bounded asynchronous dispatcher. Callbacks receive identifiers and safe
outcomes, not credentials or wallet material. Failure policy is explicit.

## Outcome relay

`xrpl_mpp_middleware.relay` provides an opt-in HTTPS relay for an allowlisted
validated receipt projection. It adds idempotency, bounds timeouts, blocks
unsafe endpoints, and rejects sensitive-key names. This is application
architecture, not part of the MPP wire contract.
