# RLUSD

RLUSD is an XRPL issued currency. Its MPP 0.2 currency value is the canonical
JSON serialization of:

```json
{
  "currency": "RLUSD",
  "issuer": "rIssuerForTheSelectedNetwork"
}
```

Do not use the former `RLUSD:rIssuer` shorthand on the 0.2 wire.

## Prerequisites

- Verify the issuer for the selected network from an authoritative source.
- Establish a trust line from the payer account.
- Fund the payer with the issued asset and enough XRP for ledger fees/reserve.
- Add the exact currency/issuer pair to the facilitator's issued-asset allowlist.
- Configure both buyer and seller for the same named network.

The built-in network constants are conveniences, not a substitute for checking
the currently intended issuer before moving value.

## Exact amount checks

Issued amounts use decimal strings and XRPL issued-amount semantics. The signer
serializes the ledger amount; the facilitator compares currency, issuer, and
numeric value and rejects partial payments. Avoid binary floating-point values
in application pricing.

Wallet funding is distinct from an MPP PaymentChannel or session action. A
faucet/helper transfer only prepares the payer's ledger balance.
