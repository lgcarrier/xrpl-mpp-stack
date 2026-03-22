# Facilitator

`xrpl-mpp-facilitator` is the settlement service behind the middleware.

It exposes:

- `GET /health`
- `GET /supported`
- `POST /charge`
- `POST /session`

The service validates XRPL signatures, enforces replay and freshness rules, and stores session balances in Redis.
