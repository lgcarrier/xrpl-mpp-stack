# `xrpl-mpp-core`

Install:

```bash
pip install xrpl-mpp-core
```

The core package owns wire compatibility. It has no network client or
settlement service.

## MPP HTTP

- `PaymentChallenge`, `PaymentCredential`, and `PaymentReceipt`
- `AcceptPaymentRange` parsing, rendering, matching, and ranking
- multiple `WWW-Authenticate: Payment` challenge extraction
- selected `Authorization` / `Payment-Authorization` credential placement
- `Payment-Receipt` codecs
- RFC 8785 canonical JSON and strict unpadded base64url
- content digests, expiry checks, and HMAC challenge key rotation
- RFC 9457-compatible payment problem details

The challenge parser preserves protocol extensibility: unknown challenge
extensions and future intent values can be read. Method-specific payload
validators remain strict.

## XRPL profile

- named `mainnet`, `testnet`, and `devnet` networks
- XRP, issued-currency, and MPT currency descriptors
- one-time charge request and transaction/hash credential payloads
- XRPL DID construction/parsing and payer binding
- deterministic InvoiceID derivation
- PaymentChannel open/voucher/close payloads
- cumulative high-water validation helpers

`XRP` is the native-currency wire value. Issued currencies and MPTs are compact
JSON strings. The pre-0.2 colon-delimited shorthand is deliberately rejected by
the 0.2 parser.

## Receipt compatibility

Code consuming a receipt should depend on `status`, `method`, `timestamp`, and
`reference`, then inspect XRPL extensions only when needed. Unknown method
extensions should not break a generic MPP client.
