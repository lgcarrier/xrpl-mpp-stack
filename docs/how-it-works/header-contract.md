# MPP HTTP header contract

The HTTP layer follows the MPP Payment authentication scheme. Header names are
case-insensitive; examples use canonical spelling.

## Client preference

```http
Accept-Payment: xrpl/charge, xrpl/session;q=0.8
```

Each range is `method/intent` with an optional quality value. Wildcards are
allowed. A quality of `0` rejects that range. Preferences help a seller order or
filter offers, but the returned challenge is always authoritative.

## Challenge

```http
HTTP/1.1 402 Payment Required
Cache-Control: no-store
WWW-Authenticate: Payment id="...", realm="merchant.example", method="xrpl", intent="charge", request="...", expires="...", header="Payment-Authorization", opaque="..."
Content-Type: application/problem+json
```

A response can contain multiple Payment challenges. The required challenge
parameters are `id`, `realm`, `method`, `intent`, and `request`. The `request`
and optional `opaque` values are unpadded base64url encodings of RFC 8785
canonical JSON. `opaque`, when present, is a flat map of string keys to string
values. `expires` is an RFC 3339 timestamp.

This stack HMAC-binds challenge fields using `opaque`. The HMAC is conditional:
generic MPP peers do not need to emit it, but a challenge issued by this stack
must verify before settlement.

## Credential field

When `header` is absent, use the default field:

```http
Authorization: Payment <base64url-jcs-credential>
```

When the challenge contains `header="Payment-Authorization"`, use exactly:

```http
Authorization: Bearer <ordinary-application-token>
Payment-Authorization: Payment <base64url-jcs-credential>
```

The selected `header` is echoed inside the credential's challenge object. Do
not copy a credential between the two fields. Selecting
`Payment-Authorization` prevents the payment exchange from replacing ordinary
application authentication.

The decoded credential has this core shape:

```json
{
  "challenge": { "id": "...", "method": "xrpl", "intent": "charge" },
  "payload": { "type": "transaction", "blob": "..." },
  "source": "did:xrpl:testnet:r..."
}
```

The full challenge is echoed, not only its ID.

## Receipt

On a successful paid response, the server should include:

```http
Payment-Receipt: <base64url-jcs-receipt>
Cache-Control: private
```

The required decoded fields are `status: "success"`, `method`, `timestamp`, and
`reference`. XRPL extensions can include `challengeId`, `network`, `payer`,
`recipient`, `invoiceId`, `channelId`, `cumulative`, `action`, `txHash`, and
`settlementStatus`.

Never emit `Payment-Receipt` on an error response. Treat receipt headers as
sensitive payment metadata in logs, caches, and browser exposure policy.
