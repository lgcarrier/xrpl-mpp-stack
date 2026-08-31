# USDC

USDC on XRPL is represented as an issued-currency descriptor. The MPP 0.2
currency string is canonical JSON with the selected network's code and issuer:

```json
{
  "currency": "USDC",
  "issuer": "rIssuerForTheSelectedNetwork"
}
```

Do not assume an issuer is identical across Mainnet, Testnet, and Devnet, and do
not use the former `USDC:rIssuer` shorthand.

## Prerequisites

1. confirm the intended issuer and currency code;
2. create the payer trust line;
3. fund the issued balance and retain XRP for reserve/fees;
4. allowlist the exact pair in the facilitator;
5. pass the same canonical currency value through discovery, challenge, signer,
   and verification policy.

If a demo funding helper is used, verify the ledger balance before running the
payment. Funding a wallet is not an MPP session refill and creates no reusable
payment credential.

The facilitator validates exact issuer, currency, amount, destination, payer,
and invoice binding and rejects partial-payment transactions.
