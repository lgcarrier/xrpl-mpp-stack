# xrpl-mpp-middleware

Seller-side ASGI middleware for MPP 0.2 challenges and authenticated facilitator
verification.

```bash
pip install xrpl-mpp-middleware
```

Public surfaces include `PaymentMiddlewareASGI`, `require_payment(...)`,
`require_session(...)`, charge/session route specs, and the facilitator client.
The middleware supports multiple challenges, `Accept-Payment`, request-digest
binding, challenge-selected `Payment-Authorization`, request-body limits, and
successful-response receipts.

Additional modules provide:

- OpenAPI discovery draft-01 augmentation for `charge` and `session`;
- immutable lifecycle hook events with bounded dispatch;
- an explicit sanitized HTTPS relay for validated payment outcomes.

Discovery is advisory; runtime `402` terms are authoritative. Hooks and relay
are optional application architecture, not MPP wire requirements. They reject
or omit payment credentials, signed blobs, wallet secrets, and bearer tokens.

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/middleware/>
