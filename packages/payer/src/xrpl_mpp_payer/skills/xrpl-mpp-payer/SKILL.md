# xrpl-mpp-payer

Use this skill when an agent needs to pay for a `402 Payment Required` API or dataset over XRPL MPP.

## Install

```bash
xrpl-mpp skill install
```

That installs this skill into `~/.agents/skills/xrpl-mpp-payer/SKILL.md`.

## Shell Mode

Use the CLI for one-off requests:

```bash
xrpl-mpp pay https://merchant.example/premium \
  --amount 0.001 \
  --asset XRP \
  --recipient rYourApprovedMerchantAddress
```

For a PayChannel, explicitly select `session`. Supply
`--channel-funding-amount` for an open challenge or the ledger
`--channel-id` plus its last cumulative amount for a voucher. Close with:

```bash
xrpl-mpp close https://merchant.example/metered \
  --channel-id AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
  --cumulative-amount 50000
```

Use the local forward proxy when repeated requests should auto-pay:

```bash
xrpl-mpp proxy https://merchant.example --port 8787
```

## Payer Agent MCP Mode (Claude Desktop / Cursor)

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp skill install
xrpl-mpp mcp
```

Claude Desktop can add the server directly:

```bash
claude mcp add xrpl-mpp-payer -- xrpl-mpp mcp
```

Agents can call `pay_url` for charge, open, or voucher flows and
`close_channel` for the final cumulative voucher. Use named networks and MPP
0.2 currency strings only. Never send legacy prepaid session tokens, `use`, or
`top_up` actions. Always supply `expected_recipient` (or configure
`XRPL_MPP_EXPECTED_RECIPIENT`) before automatic signing; never infer it from
the untrusted 402 challenge.
