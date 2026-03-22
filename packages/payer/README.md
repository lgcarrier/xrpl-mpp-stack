# xrpl-mpp-payer

CLI, proxy, and MCP payer for XRPL-backed MPP HTTP resources.

## Install

```bash
pip install xrpl-mpp-payer
```

For MCP support:

```bash
pip install "xrpl-mpp-payer[mcp]"
```

## Commands

```bash
xrpl-mpp pay https://merchant.example/premium --amount 0.001 --asset XRP
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp skill install
xrpl-mpp mcp
```

The payer stores receipts locally, enforces spend caps with `XRPL_MPP_MAX_SPEND`, and can auto-pay `charge` or `session` flows over HTTP.

## Environment

- `XRPL_WALLET_SEED` is required for real payments
- `XRPL_RPC_URL` overrides the JSON-RPC endpoint
- `XRPL_NETWORK` selects the CAIP-2 XRPL network id
- `XRPL_MPP_MAX_SPEND` sets a global spend cap
- `XRPL_MPP_RECEIPTS_PATH` overrides the local receipts file path

`xrpl-mpp pay --dry-run ...` is useful for confirming that a route exposes a valid MPP challenge before you allow the payer to sign and retry automatically.
