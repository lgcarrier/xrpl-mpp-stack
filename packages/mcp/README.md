# xrpl-mpp-mcp

Framework-neutral implementation of the native MPP JSON-RPC/MCP payment
transport.

```bash
pip install xrpl-mpp-mcp
```

The package provides capability advertisement; root and MCP-nested `_meta`
handling; operation-bound challenges and request hashes; replay-safe callback
processing; successful receipt metadata; and the exact payment error mapping:

- `-32042` payment required
- `-32043` verification failed
- `-32602` malformed credential
- `-32603` processor failure

Paid operations are `tools/call`, `resources/read`, and `prompts/get`.
Credentials use `org.paymentauth/credential`; receipts use
`org.paymentauth/receipt`. Conflicting metadata placement is rejected and paid
notifications that cannot receive a response are dropped.

This package is not an MCP server framework and does not settle XRPL payments by
itself. Inject application verification and settlement callbacks.

Documentation: <https://lgcarrier.github.io/xrpl-mpp-stack/packages/mcp/>
