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

## Create and fund a new Testnet wallet

Use the browser-free funding command to create an XRPL Testnet wallet with XRP
and official Testnet RLUSD. It does not connect a wallet in a browser, retain a
GitHub session, or require Playwright.

From the repository root with the virtual environment active:

```bash
python -m devtools.rlusd_fund \
  --new-wallet \
  --target-rlusd 10 \
  --max-xrp 35
```

The command persists the seed before requesting XRP, creates the official RLUSD
trust line, quotes an XRP-to-RLUSD route, and submits an exact-output circular
Payment. `--max-xrp` is the absolute XRP `SendMax` cap for the conversion, not
a request for that exact faucet balance.

Output includes the public address, validated XRP and RLUSD balances,
transaction hashes, and a private wallet-file path. It never prints the seed.
Wallet and recovery state live under the Git-ignored `.live-test-wallets/`
directory with private permissions.

## Resume safely

If the command reports `pending` and exits with status `3`, rerun it with the
exact wallet path it printed:

```bash
python -m devtools.rlusd_fund \
  --wallet-file .live-test-wallets/rlusd-funded-wallet-YYYYMMDDTHHMMSSZ.json \
  --target-rlusd 10 \
  --max-xrp 35
```

The retry checks the journaled transaction hash first and only rebroadcasts the
same signed blob while it remains live. It does not sign another conversion
until the previous transaction validates or is authoritatively absent after
expiry. Target-balance semantics also make a completed rerun a no-op.

## Fund the cached demo buyer

After the [Testnet XRP quickstart](../quickstart/testnet-xrp.md), omit both
wallet-selection options to fund the dedicated cached RLUSD buyer:

```bash
python -m devtools.rlusd_fund --target-rlusd 10 --max-xrp 35
```

If this cached-wallet command exits with status `3`, rerun the same command
without `--wallet-file`; the multi-wallet cache is not a standalone wallet
file. The command is Testnet-only, uses the repository's official Testnet
issuer, caps every transaction fee, rejects partial payments, waits for a
validated `tesSUCCESS`, and checks the final RLUSD balance. Exit status `2`
means current liquidity or the configured XRP cap prevented funding; exit
status `1` means validation or infrastructure failed.

## Prerequisites

- For Testnet, use Ripple's current issuer
  `rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV`, as listed in the
  [official RLUSD documentation](https://docs.ripple.com/products/stablecoin/developer-resources/rlusd-on-the-xrpl).
  The former `rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2` issuer is retained only as a
  legacy constant and is rejected when configured as RLUSD.
- Verify the issuer for any other selected network from an authoritative
  source.
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

## Switch the demo to RLUSD

Generate a derived env file after funding the cached buyer:

```bash
python -m devtools.demo_env --asset rlusd
```

That writes `.env.quickstart.rlusd` with RLUSD merchant pricing, the dedicated
buyer seed, buyer asset selection, and any facilitator-side
`ALLOWED_ISSUED_ASSETS` entry required for the selected issuer. Restart the
stack and run the buyer with that file:

```bash
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

The merchant prices `/premium` in RLUSD, and the buyer selects the matching
canonical issued-currency payment option.

## Legacy top-up helper

`python -m devtools.rlusd_topup` remains available for recovering or using the
older `TRYRLUSD_SESSION_TOKEN` faucet workflow, but it is no longer the
recommended way to acquire Testnet RLUSD. Prefer `devtools.rlusd_fund` for new
wallets and the cached demo buyer because it has bounded on-ledger conversion
and crash-safe reconciliation.
