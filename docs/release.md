# Release playbook

The 0.2 stack publishes six Python packages:

- `xrpl-mpp-core`
- `xrpl-mpp-facilitator`
- `xrpl-mpp-middleware`
- `xrpl-mpp-client`
- `xrpl-mpp-payer`
- `xrpl-mpp-mcp`

## Version and dependency rules

Use one coordinated stack version such as `0.2.0`. Internal package ranges must
accept that release and must not resolve to the incompatible 0.1 line. Verify
the version in each `pyproject.toml` and generated/runtime version module.

Publish core before packages that depend on it. Publish client before payer.
The MCP transport package is intentionally framework-neutral and has no stack
package dependency, but should use the coordinated release tag for operator
clarity.

Every wheel must carry `LICENSE` under its own `.dist-info/licenses/`
directory. Do not force-include a shared top-level `site-packages/LICENSE`,
because uninstalling one stack distribution can then remove another's file.

All six build backends use `hatchling>=1.27,<1.33`. Hatchling 1.32 emits core
metadata 2.5, so release validation requires Twine 7 or newer; keep the package
constraints and the development tooling in sync when either dependency moves.

## Required verification

```bash
pytest
PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall packages tests examples devtools conformance
mkdocs build --strict
```

Build every package, including MCP:

```bash
for package in packages/core packages/facilitator packages/middleware packages/client packages/payer packages/mcp; do
  ( cd "$package" && python -m build --sdist && python -m build --wheel )
done
twine check packages/*/dist/*
```

Also run the official `mpp-tools` vector and flow conformance adapter pinned by
the repository. The conformance workflow must retain a successful upstream
`json_rpc_mcp_payment` fixture sanity check and separately execute that pinned
fixture through `xrpl-mpp-mcp`; the upstream runner's JSON-RPC case does not
invoke the selected adapter. It must also byte-match the cross-language Ripple
SDK fixtures at pinned commit `6907484c92d217da406e2f3d7b5e6587703c6ea8`.
Smoke the facilitator CLI, install built artifacts into clean environments,
and build the Docker image.

The live XRPL Testnet path is required when settlement, replay, signer, or
ledger validation changes and credentials/network access are available:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
```

Record an unavailable faucet/RPC as a blocked external verification, not as a
passing test.

## Tagging and trusted publishing

Follow the tag form enforced by `.github/workflows/publish-package.yml`:

```text
core-v0.2.0
facilitator-v0.2.0
middleware-v0.2.0
client-v0.2.0
payer-v0.2.0
mcp-v0.2.0
```

Pre-create and review the exact GitHub environments selected by the workflow:

| Package target | TestPyPI environment | PyPI environment |
| --- | --- | --- |
| `core` | `testpypi-core` | `pypi-core` |
| `facilitator` | `testpypi-facilitator` | `pypi-facilitator` |
| `middleware` | `testpypi-middleware` | `pypi-middleware` |
| `client` | `testpypi-client` | `pypi-client` |
| `payer` | `testpypi-payer` | `pypi-payer` |
| `mcp` | `testpypi-mcp` | `pypi-mcp` |

Each environment must match a trusted publisher on its target index. For a new
project such as `xrpl-mpp-mcp`, configure the pending publisher on TestPyPI and
PyPI before its first workflow run; creating the GitHub environment alone does
not reserve the package or authorize an upload.

Before pushing a tag, verify it points at the reviewed commit, the tag version
matches package metadata, trusted-publisher environments target the correct
PyPI project, and no stale `dist/` artifact is included.

The publish workflow must remain gated on the full local verification job and
the reusable pinned conformance workflow. A package tag must never bypass
those checks and publish directly after only building metadata.

## Post-publish checks

Install exact versions from TestPyPI/PyPI into clean environments, import the
public package, smoke CLI entry points, and verify optional payer agent extras
separately. Confirm source and wheel metadata with `twine check` and verify the
published docs describe the same wire contract.

For TestPyPI, download the exact target and coordinated internal stack wheels
from TestPyPI with dependencies disabled, then install those local artifacts
while resolving only third-party dependencies from PyPI. Never use PyPI as an
extra index for selecting the artifact under test.

## 0.2 release note warning

Call out the clean break prominently: named networks, canonical currency
descriptors, selected payment header, cumulative PaymentChannel proofs, and the
four-field receipt core. Operators must upgrade buyers, sellers, facilitator,
and persisted protocol state together.
