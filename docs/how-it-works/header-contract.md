# Header Contract

The stack speaks MPP HTTP over three standard headers:

- `WWW-Authenticate: Payment ...`
- `Authorization: Payment <token>`
- `Payment-Receipt: <token>`

## Challenge

Unpaid requests receive `402 Payment Required` with one or more `WWW-Authenticate: Payment` headers. Each challenge includes:

- `id`
- `realm`
- `method="xrpl"`
- `intent="charge"` or `intent="session"`
- `request`
- optional `digest`, `expires`, `description`, and `opaque`

The `request` value is base64url-encoded canonical JSON. For XRPL charge routes it contains:

- `amount`
- `currency`
- `recipient`
- `methodDetails.network`
- `methodDetails.invoiceId`

For session routes it contains:

- `amount`
- `currency`
- `recipient`
- `methodDetails.network`
- `methodDetails.sessionId`
- `methodDetails.unitAmount`
- `methodDetails.minPrepayAmount`

In this release, fixed-price session routes require `methodDetails.unitAmount` to match `amount`.

## Authorization

Paid retries send:

```http
Authorization: Payment <base64url-jcs-credential>
```

The credential contains the selected challenge plus a method-specific payload:

- `charge`: `signedTxBlob`
- `session open`: `action="open"` and `signedTxBlob`
- `session use`: `action="use"` and `sessionToken`
- `session top_up`: `action="top_up"`, `sessionToken`, and `signedTxBlob`
- `session close`: `action="close"` and `sessionToken`

## Receipt

Successful paid responses include:

```http
Payment-Receipt: <base64url-jcs-receipt>
```

Receipts include common fields such as:

- `method`
- `timestamp`
- `reference`
- `intent`
- `network`
- `payer`
- `recipient`

Charge receipts also include `invoiceId`, `txHash`, `settlementStatus`, `asset`, and `amount`.

Session receipts may include `sessionId`, `sessionToken`, `spentTotal`, `availableBalance`, `prepaidTotal`, and `lastAction`.
