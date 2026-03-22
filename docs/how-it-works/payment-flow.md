# Payment Flow

## Charge

```mermaid
sequenceDiagram
    participant Buyer
    participant Middleware
    participant Facilitator
    participant App as Seller App

    Buyer->>Middleware: Request protected route
    Middleware-->>Buyer: 402 + WWW-Authenticate: Payment (charge)
    Buyer->>Buyer: Sign XRPL Payment with challenge invoiceId
    Buyer->>Middleware: Retry with Authorization: Payment
    Middleware->>Facilitator: POST /charge
    Facilitator->>Facilitator: Validate and settle XRPL transaction
    Facilitator-->>Middleware: PaymentReceipt
    Middleware->>App: Forward paid request
    App-->>Middleware: Protected response
    Middleware-->>Buyer: 200 + Payment-Receipt
```

1. A buyer requests a protected resource.
2. The middleware returns `402 Payment Required` with `WWW-Authenticate: Payment`.
3. The buyer decodes the challenge request and signs an XRPL `Payment`.
4. The buyer retries with `Authorization: Payment`.
5. The middleware forwards the credential to the facilitator.
6. The facilitator validates and settles the XRPL transaction.
7. The app receives `request.state.mpp_payment`, and the response includes `Payment-Receipt`.

## Session

```mermaid
sequenceDiagram
    participant Buyer
    participant Middleware
    participant Facilitator
    participant App as Seller App

    Buyer->>Middleware: Request session-protected route
    Middleware-->>Buyer: 402 + WWW-Authenticate: Payment (session)
    Buyer->>Buyer: Sign XRPL prepay with challenge sessionId
    Buyer->>Middleware: Authorization: Payment (open) + X-MPP-Session-Id
    Middleware->>Facilitator: POST /session (action=open)
    Facilitator-->>Middleware: Session receipt + sessionToken
    Middleware->>App: Forward paid request
    App-->>Middleware: Protected response
    Middleware-->>Buyer: 200 + Payment-Receipt

    Note over Buyer,Facilitator: Later requests reuse the same session token
    Buyer->>Middleware: Authorization: Payment (use)
    Middleware->>Facilitator: POST /session (action=use)
    Facilitator-->>Middleware: Usage receipt
    Middleware->>App: Forward paid request
    App-->>Middleware: Protected response
    Middleware-->>Buyer: 200 + Payment-Receipt

    opt Session balance too low
        Buyer->>Middleware: Authorization: Payment (top_up)
        Middleware->>Facilitator: POST /session (action=top_up)
        Facilitator->>Facilitator: Validate XRPL top-up transaction
        Facilitator-->>Middleware: Updated session receipt
        Middleware-->>Buyer: 200 + Payment-Receipt
    end

    opt Session finished
        Buyer->>Middleware: Authorization: Payment (close)
        Middleware->>Facilitator: POST /session (action=close)
        Facilitator-->>Middleware: Closed session receipt
        Middleware-->>Buyer: 200 + Payment-Receipt
    end
```

1. A buyer requests a session-protected resource.
2. The middleware returns a `session` challenge.
3. The buyer opens the session with a prepaid XRPL transaction.
4. Later requests reuse the session with `action="use"`.
5. If balance runs low, the buyer sends `action="top_up"`.
6. When finished, the buyer sends `action="close"`.
