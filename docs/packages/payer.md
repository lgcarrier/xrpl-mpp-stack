# Payer

`xrpl-mpp-payer` is the operator-facing runtime for paying protected MPP resources.

## Use It When

Use this package when you want a ready-made buyer:

- a CLI for manual payments and previews
- a local proxy that auto-pays upstream protected routes
- a local receipt store and spend-budget guardrails
- an MCP bridge for agent tooling

## Install

```bash
pip install xrpl-mpp-payer
```

For MCP support:

```bash
pip install "xrpl-mpp-payer[mcp]"
```

## Commands

Commands:

- `xrpl-mpp pay`
- `xrpl-mpp proxy`
- `xrpl-mpp receipts`
- `xrpl-mpp budget`
- `xrpl-mpp skill install`
- `xrpl-mpp mcp`

Receipts are stored locally and spend caps are controlled with `XRPL_MPP_MAX_SPEND`.

## Common Usage

```bash
xrpl-mpp pay https://merchant.example/premium --amount 0.001 --asset XRP
xrpl-mpp pay https://merchant.example/premium --dry-run
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp receipts --limit 20
xrpl-mpp budget --asset XRP
```

Use `--dry-run` when you want to confirm that a route exposes a valid MPP challenge
before allowing the payer to sign and retry automatically.

## Environment

The payer reads:

- `XRPL_WALLET_SEED` for real payments
- `XRPL_RPC_URL` for the buyer JSON-RPC endpoint
- `XRPL_NETWORK` for challenge matching and signing
- `XRPL_MPP_MAX_SPEND` for default budget enforcement
- `XRPL_MPP_RECEIPTS_PATH` for receipt-store location overrides

If no network is configured, the payer falls back to `xrpl:1`, which makes it
friendly for local Testnet demos but should be set explicitly for production use.

## MCP And Agent Tooling

```bash
xrpl-mpp skill install
xrpl-mpp mcp
```

Claude Desktop can register it directly:

```bash
claude mcp add xrpl-mpp-payer -- xrpl-mpp mcp
```

This is the shortest path to giving local agents a safe XRPL-backed payment bridge
without teaching them your application-specific buyer code.
