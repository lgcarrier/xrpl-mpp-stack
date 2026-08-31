# xrpl-python conformance adapter

This directory adapts `xrpl-mpp-core` to the schema-backed JSON ABI in
[`tempoxyz/mpp-tools`](https://github.com/tempoxyz/mpp-tools). The adapter is
kept in this repository so conformance runs exercise the checked-out source,
not a previously published package.

The CI workflow pins mpp-tools to commit
`b6b07aac36973ca6cf2c7dc9d4e43696cee0bfc5`, links this directory into its
`conformance/adapters/` directory, and runs the core vectors and HTTP flow
suite. The pinned flow runner handles `json_rpc_mcp_payment` against its own
compliance server; although the report carries the selected adapter label,
that check does not invoke this adapter or `xrpl-mpp-mcp`. CI requires it only
as an upstream fixture sanity check, so a rename, skip, or removal cannot pass
silently.

Package coverage is a separate gate. CI loads `json_rpc_mcp_payment` from the
pinned `conformance/flows/flows.json` and passes its payload through
`xrpl-mpp-mcp`'s paid-operation wrapper. That check verifies the initial
`-32042` challenge, the fixture's root `_meta` credential retry, the nested
receipt, operation binding, application dispatch, and replay rejection.

The pinned upstream suite predates the August 25, 2026 alternate credential
header vectors. Its protocol schema does not yet admit `header` on challenges
or challenge-ID inputs. Consequently, passing the mpp-tools adapter checks is
necessary but not sufficient: repository tests remain the release gate for
`Payment-Authorization`, its conditional eighth HMAC slot after `opaque`,
credential echoing, and coexistence with ordinary `Authorization` values.

The adapter intentionally does not advertise method-specific Tempo or Stripe
operations, `http.payment_request`, or `server.verify`. Its advertised paths
exercise the shared challenge, credential, receipt, replay, cache, digest, and
Problem Details behavior. Native tests cover XRPL-specific settlement, the
production HTTP client, discovery, and the full MCP transport surface.

The pinned JSON-RPC fixture represents one `tools/call` challenge, credential,
and receipt round trip. The package-driven gate intentionally reuses that
fixture, but it does not cover MCP resources, prompts, every JSON-RPC error
path, or payer policy; those remain covered by the repository's native MCP and
payer tests.
