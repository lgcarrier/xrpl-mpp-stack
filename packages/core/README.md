# xrpl-mpp-core

Shared MPP 0.2 HTTP wire models, RFC 8785/base64url codecs, challenge binding,
and Ripple-compatible XRPL charge and PaymentChannel contracts.

```bash
pip install xrpl-mpp-core
```

Highlights:

- `PaymentChallenge`, `PaymentCredential`, and extensible `PaymentReceipt`
- multiple Payment challenges and `Accept-Payment` negotiation
- `Authorization` / `Payment-Authorization` selection
- challenge expiry, digest, HMAC, and key-rotation helpers
- named XRPL networks and canonical XRP/issued-currency/MPT descriptors
- XRPL DID, InvoiceID, exact charge, and cumulative channel models

The package contains no ledger client or settlement service. Version 0.2 does
not accept the repository's former CAIP-like network values, colon-delimited
asset identifiers, or reusable prepaid session wire objects.

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/core/>
