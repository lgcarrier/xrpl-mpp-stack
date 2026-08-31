from __future__ import annotations

import pytest

from conformance.verify_mcp_transport_fixture import (
    load_json_rpc_mcp_fixture,
    verify_mcp_transport_fixture,
)
from conformance.verify_mpp_tools_flow_report import verify_upstream_fixture_report


def _report(*, report_status: str = "pass", check_status: str = "SUCCESS"):
    return {
        "status": report_status,
        "checks": [
            {
                "status": check_status,
                "details": {
                    "adapter": "xrpl-python",
                    "flow": "json_rpc_mcp_payment",
                },
            }
        ],
    }


def _fixture_document(*, payload=None):
    return {
        "version": "1.0.0",
        "cases": [
            {
                "name": "json_rpc_mcp_payment",
                "path": "/rpc",
                "json_rpc": True,
                "payload": payload
                if payload is not None
                else {"type": "transaction", "signature": "0xjsonrpc"},
            }
        ]
    }


def test_upstream_flow_gate_accepts_the_pinned_runner_check() -> None:
    verify_upstream_fixture_report(_report())


@pytest.mark.parametrize(
    ("report", "message"),
    [
        (_report(report_status="fail"), "report did not pass"),
        (_report(check_status="FAILURE"), "check did not succeed"),
        ({"status": "pass", "checks": []}, "exactly one"),
        (
            {
                "status": "pass",
                "checks": [
                    {
                        "status": "SUCCESS",
                        "details": {
                            "adapter": "typescript",
                            "flow": "json_rpc_mcp_payment",
                        },
                    }
                ],
            },
            "exactly one",
        ),
    ],
)
def test_upstream_flow_gate_fails_closed(report, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        verify_upstream_fixture_report(report)


def test_mcp_transport_gate_executes_the_pinned_fixture_shape() -> None:
    verify_mcp_transport_fixture(_fixture_document())


def test_mcp_transport_gate_accepts_a_top_level_flow_array() -> None:
    document = _fixture_document()
    fixture = load_json_rpc_mcp_fixture(document["cases"])

    assert fixture["name"] == "json_rpc_mcp_payment"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"version": "1.0.0", "cases": []}, "exactly one"),
        (
            {
                "version": "1.0.0",
                "cases": [
                    *_fixture_document()["cases"],
                    *_fixture_document()["cases"],
                ]
            },
            "exactly one",
        ),
        (
            {
                "version": "1.0.0",
                "cases": [
                    {**_fixture_document()["cases"][0], "json_rpc": False}
                ]
            },
            "must remain a JSON-RPC fixture",
        ),
        (_fixture_document(payload="not-an-object"), "payload must be a JSON object"),
    ],
)
def test_mcp_transport_gate_fails_closed(document, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_json_rpc_mcp_fixture(document)
