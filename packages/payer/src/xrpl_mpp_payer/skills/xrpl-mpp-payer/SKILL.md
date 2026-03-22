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
xrpl-mpp pay https://merchant.example/premium --amount 0.001 --asset XRP
```

Use the local forward proxy when repeated requests should auto-pay:

```bash
xrpl-mpp proxy https://merchant.example --port 8787
```

## Native MCP Mode (Claude Desktop / Cursor)

```bash
pip install "xrpl-mpp-payer[mcp]"
xrpl-mpp skill install
xrpl-mpp mcp
```

Claude Desktop can add the server directly:

```bash
claude mcp add xrpl-mpp-payer -- xrpl-mpp mcp
```

Agents can call `pay_url` directly in MCP mode without shelling out to the CLI.
