# `xrpl-mpp-mcp`

Install:

```bash
pip install xrpl-mpp-mcp
```

This framework-neutral package implements the native MPP JSON-RPC/MCP
transport. It does not provide an MCP server framework or XRPL settlement
backend.

## Wire behavior

- advertises payment method/intent capabilities;
- supports `tools/call`, `resources/read`, and `prompts/get`;
- extracts root-level JSON-RPC or nested MCP `_meta`;
- rejects conflicting metadata placement;
- uses `org.paymentauth/credential` and `org.paymentauth/receipt`;
- binds challenge IDs and request hashes to the exact paid operation;
- drops paid notifications that cannot receive a response;
- provides replay-safe callback processing.

## Errors

| Condition | Code |
| --- | ---: |
| payment required | `-32042` |
| verification failed | `-32043` |
| malformed credential | `-32602` |
| processor failure | `-32603` |

Verification failures include a fresh challenge. Successful paid operations
include a receipt in response metadata.

The package deliberately does not infer XRPL settlement from JSON-RPC. Inject a
processor that verifies a credential using the same method contracts and
replay rules as the HTTP stack.
