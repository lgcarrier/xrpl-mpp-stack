#!/usr/bin/env python3
"""Verify the pinned mpp-tools runner's JSON-RPC fixture sanity check.

The pinned runner executes ``json_rpc_mcp_payment`` against its own compliance
server rather than through the selected adapter. This verifier intentionally
checks only that the upstream fixture remains present and passing; repository
MCP-package coverage is enforced separately by
``verify_mcp_transport_fixture.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


EXPECTED_RUNNER_LABEL = "xrpl-python"
EXPECTED_UPSTREAM_FLOW = "json_rpc_mcp_payment"


def verify_upstream_fixture_report(report: Mapping[str, Any]) -> None:
    if report.get("status") != "pass":
        raise ValueError("mpp-tools flow report did not pass")

    checks = report.get("checks")
    if not isinstance(checks, list):
        raise ValueError("mpp-tools flow report is missing checks")

    matches: list[Mapping[str, Any]] = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        details = check.get("details")
        if not isinstance(details, Mapping):
            continue
        if (
            details.get("adapter") == EXPECTED_RUNNER_LABEL
            and details.get("flow") == EXPECTED_UPSTREAM_FLOW
        ):
            matches.append(check)

    if len(matches) != 1:
        raise ValueError(
            "mpp-tools report must contain exactly one upstream fixture check "
            f"labelled {EXPECTED_RUNNER_LABEL}/{EXPECTED_UPSTREAM_FLOW}; "
            f"found {len(matches)}"
        )
    if matches[0].get("status") != "SUCCESS":
        raise ValueError("mpp-tools upstream JSON-RPC fixture check did not succeed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping):
        raise ValueError("mpp-tools flow report must be a JSON object")
    verify_upstream_fixture_report(report)
    print(
        "verified upstream mpp-tools fixture "
        f"{EXPECTED_RUNNER_LABEL}/{EXPECTED_UPSTREAM_FLOW}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
