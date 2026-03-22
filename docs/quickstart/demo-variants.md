# Run Demo Variants

This page is the fastest way to run the three supported demo variants from one place:

- XRP
- RLUSD
- USDC

If you have not generated the base quickstart env yet, start with
[Guided Quickstart: Testnet XRP](testnet-xrp.md).

## One-Time Setup

Create the base quickstart env and wallet cache:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m devtools.quickstart
```

That writes `.env.quickstart` and caches dedicated buyer wallets for XRP, RLUSD,
and USDC.

## Demo Matrix

| Demo | Base helper | Derived env file | Buyer asset | Extra prep |
| --- | --- | --- | --- | --- |
| XRP | `python -m devtools.quickstart` | `.env.quickstart` | `XRP:native` | None |
| RLUSD | `python -m devtools.demo_env --asset rlusd` | `.env.quickstart.rlusd` | `RLUSD:<issuer>` | Run `python -m devtools.rlusd_topup` first |
| USDC | `python -m devtools.demo_env --asset usdc` | `.env.quickstart.usdc` | `USDC:<issuer>` | Run `python -m devtools.usdc_topup` first |

## XRP Demo

Use the base quickstart env directly:

```bash
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart --profile demo run --rm buyer
```

Expected result:

```text
status=200
{"message":"premium content unlocked", ...}
```

## RLUSD Demo

First, prepare RLUSD:

```bash
export TRYRLUSD_SESSION_TOKEN=...
python -m devtools.rlusd_topup
```

Then generate the derived env and run the stack:

```bash
python -m devtools.demo_env --asset rlusd
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

That derived env file updates:

- `PRICE_ASSET_CODE`
- `PRICE_ASSET_ISSUER`
- `PRICE_ASSET_AMOUNT`
- `PAYMENT_ASSET`
- `XRPL_WALLET_SEED`
- `ALLOWED_ISSUED_ASSETS` when a non-built-in issuer is used

For faucet and claim-recovery details, see [RLUSD Guide](../asset-guides/rlusd.md).

## USDC Demo

First, prepare USDC:

```bash
python -m devtools.usdc_topup
```

If the helper asks for a manual Circle faucet claim, complete that first and rerun the helper.

Then generate the derived env and run the stack:

```bash
python -m devtools.demo_env --asset usdc
docker compose --env-file .env.quickstart.usdc up --build
docker compose --env-file .env.quickstart.usdc --profile demo run --rm buyer
```

For faucet and sweep details, see [USDC Guide](../asset-guides/usdc.md).

## Optional Trace And Agent Modes

To watch the demo buyer trace inside Docker:

```bash
docker compose --env-file .env.quickstart --profile demo run --rm buyer
```

To run the MCP bridge instead:

```bash
docker compose --env-file .env.quickstart --profile buyer-agent-mcp up --build buyer-agent-mcp
```

## Cleanup

Stop the stack:

```bash
docker compose --env-file .env.quickstart down
docker compose --env-file .env.quickstart.rlusd down
docker compose --env-file .env.quickstart.usdc down
```

You can reuse the generated env files and cached buyer wallets between runs.
