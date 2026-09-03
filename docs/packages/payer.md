# `xrpl-mpp-payer`

Install:

```bash
pip install xrpl-mpp-payer
xrpl-mpp --help
```

Install and run the optional payer agent server with:

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp mcp
```

The payer package wraps the client SDK for operators and agents. It provides a
CLI, local auto-pay proxy, spend policy, and local receipt history.

Typical commands include:

```bash
export XRPL_MPP_EXPECTED_RECIPIENT=rYourApprovedMerchantAddress
xrpl-mpp pay https://merchant.example/premium --dry-run
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp receipts
xrpl-mpp budget
```

Use environment variables or a secret manager for the wallet seed, trusted
network/RPC endpoint, allowed recipient and currency, and spend cap. Dry-run
mode is the safest way to inspect a challenge before enabling signing.

Automatic signing fails closed until the operator supplies an approved
recipient with `--recipient` or `XRPL_MPP_EXPECTED_RECIPIENT`. The value is a
policy input, not a default copied from the challenge. Apply
`XRPL_MPP_MAX_SPEND` as a second independent ceiling for unattended use.

The `close` command sends a final cumulative voucher. It does not perform the
funder's on-ledger `tfClose` transaction or refund unused channel funding.

## Agent integration

The payer's optional agent server is an operational wrapper around buyer
behavior. The standalone `xrpl-mpp-mcp` package is the protocol implementation
for native MPP metadata inside an MCP server. They solve different problems:

- use `xrpl-mpp-payer` to give an agent a controlled payer capability;
- use `xrpl-mpp-mcp` to make an MCP operation itself payment-aware.

Never grant an agent an unlimited funded wallet. Apply recipient, currency,
network, and spend limits outside model-generated arguments as well as inside
the payer.

The payer agent tools do not expose recipient or spend-cap overrides to the
model. Non-dry-run `pay_url`, `close_channel`, and `proxy_mode` require
`XRPL_MPP_EXPECTED_RECIPIENT` and `XRPL_MPP_MAX_SPEND` in operator-controlled
process configuration. The environment value is an independent ceiling and
cannot be raised by a tool call.
