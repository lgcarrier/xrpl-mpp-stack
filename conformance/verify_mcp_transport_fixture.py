#!/usr/bin/env python3
"""Run xrpl-mpp-mcp against mpp-tools' pinned JSON-RPC payment fixture."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from xrpl_mpp_mcp import (
    CREDENTIAL_META_KEY,
    RECEIPT_META_KEY,
    MCPPaymentChallenge,
    MCPPaymentCredential,
    MCPPaymentReceipt,
    PaidOperationBinding,
    PaidOperationContext,
    PaymentErrorCode,
    PaymentVerificationFailed,
    build_bound_challenge,
    paid_operation,
    verify_bound_challenge,
)


EXPECTED_FLOW = "json_rpc_mcp_payment"
EXPECTED_PATH = "/rpc"
CHALLENGE_SECRET = "mpp-tools-pinned-fixture-secret"
CHALLENGE_REQUEST = {
    "amount": "1",
    "currency": "USD",
    "recipient": "merchant",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_json_rpc_mcp_fixture(document: Any) -> Mapping[str, Any]:
    """Select the one pinned ``json_rpc_mcp_payment`` flow definition."""

    flows: Any
    if isinstance(document, list):
        flows = document
    elif isinstance(document, Mapping):
        flows = document.get("cases")
    else:
        flows = None
    if not isinstance(flows, list):
        raise ValueError("mpp-tools fixture document must contain a cases array")

    matches = [
        flow
        for flow in flows
        if isinstance(flow, Mapping) and flow.get("name") == EXPECTED_FLOW
    ]
    if len(matches) != 1:
        raise ValueError(
            f"mpp-tools fixtures must contain exactly one {EXPECTED_FLOW} flow; "
            f"found {len(matches)}"
        )

    fixture = matches[0]
    if fixture.get("json_rpc") is not True:
        raise ValueError(f"{EXPECTED_FLOW} must remain a JSON-RPC fixture")
    if fixture.get("path") != EXPECTED_PATH:
        raise ValueError(f"{EXPECTED_FLOW} must target {EXPECTED_PATH}")
    if not isinstance(fixture.get("payload"), Mapping):
        raise ValueError(f"{EXPECTED_FLOW} payload must be a JSON object")
    return fixture


def _verify_paid_operation(operation: PaidOperationBinding) -> None:
    _require(operation.method == "tools/call", "fixture method was not tools/call")
    _require(operation.target == "paid", "fixture target was not the paid tool")


@dataclass(slots=True)
class _FixturePaymentProcessor:
    payload: dict[str, Any]
    consumed_challenges: set[str] = field(default_factory=set)
    challenge_calls: int = 0
    validation_calls: int = 0

    async def create_challenges(
        self,
        operation: PaidOperationBinding,
    ) -> Sequence[MCPPaymentChallenge]:
        _verify_paid_operation(operation)
        self.challenge_calls += 1
        return [
            build_bound_challenge(
                secret=CHALLENGE_SECRET,
                realm="conformance",
                payment_method="tempo",
                intent="charge",
                request=CHALLENGE_REQUEST,
                operation=operation,
                expires="2099-01-01T00:00:00Z",
            )
        ]

    async def validate_and_consume(
        self,
        credential: MCPPaymentCredential,
        operation: PaidOperationBinding,
    ) -> MCPPaymentReceipt:
        _verify_paid_operation(operation)
        self.validation_calls += 1
        if credential.payload != self.payload:
            raise PaymentVerificationFailed(
                reason="fixture-payload-mismatch",
                detail="credential did not preserve the pinned fixture payload",
            )
        if not verify_bound_challenge(
            credential.challenge,
            operation,
            secrets=CHALLENGE_SECRET,
        ):
            raise PaymentVerificationFailed(
                reason="operation-binding-mismatch",
                detail="challenge is not bound to this paid operation",
            )
        if credential.challenge.id in self.consumed_challenges:
            raise PaymentVerificationFailed(
                reason="replayed-credential",
                detail="fixture challenge was already consumed",
            )
        self.consumed_challenges.add(credential.challenge.id)
        return MCPPaymentReceipt(
            status="success",
            method=credential.challenge.method,
            timestamp="2026-01-01T00:00:00Z",
            challengeId=credential.challenge.id,
            reference="mpp-tools-json-rpc-fixture",
        )


async def _verify_fixture_round_trip(fixture: Mapping[str, Any]) -> None:
    fixture_payload = dict(fixture["payload"])
    processor = _FixturePaymentProcessor(payload=fixture_payload)
    application_calls = 0

    @paid_operation(processor, expected_method="tools/call")
    async def paid_tool(context: PaidOperationContext) -> Mapping[str, Any]:
        nonlocal application_calls
        _verify_paid_operation(context.operation)
        application_calls += 1
        return {"content": [{"type": "text", "text": "paid"}]}

    initial_message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "paid"},
    }
    challenge_response = await paid_tool(initial_message)
    _require(challenge_response is not None, "paid request returned no response")
    challenge_error = challenge_response.get("error")
    _require(isinstance(challenge_error, Mapping), "paid request did not return an error")
    _require(
        challenge_error.get("code") == int(PaymentErrorCode.PAYMENT_REQUIRED),
        "paid request did not return the payment-required code",
    )
    challenge_data = challenge_error.get("data")
    _require(isinstance(challenge_data, Mapping), "payment error data is missing")
    _require(challenge_data.get("httpStatus") == 402, "payment error is not HTTP 402")
    challenges = challenge_data.get("challenges")
    _require(
        isinstance(challenges, list) and len(challenges) == 1,
        "payment error must contain exactly one challenge",
    )
    challenge = challenges[0]
    _require(isinstance(challenge, Mapping), "payment challenge is not a JSON object")

    retry_message = {
        **initial_message,
        "_meta": {
            CREDENTIAL_META_KEY: {
                "challenge": dict(challenge),
                "payload": fixture_payload,
            }
        },
    }
    success_response = await paid_tool(retry_message)
    _require(success_response is not None, "paid retry returned no response")
    _require("error" not in success_response, "paid retry returned a JSON-RPC error")
    result = success_response.get("result")
    _require(isinstance(result, Mapping), "paid retry result is not a JSON object")
    _require(
        result.get("content") == [{"type": "text", "text": "paid"}],
        "paid retry did not invoke the application handler",
    )
    result_metadata = result.get("_meta")
    _require(isinstance(result_metadata, Mapping), "paid result metadata is missing")
    receipt = result_metadata.get(RECEIPT_META_KEY)
    _require(isinstance(receipt, Mapping), "paid result receipt is missing")
    _require(receipt.get("status") == "success", "receipt status is not success")
    _require(receipt.get("method") == challenge.get("method"), "receipt method changed")
    _require(
        receipt.get("challengeId") == challenge.get("id"),
        "receipt challengeId changed",
    )

    replay_response = await paid_tool(retry_message)
    _require(replay_response is not None, "replayed request returned no response")
    replay_error = replay_response.get("error")
    _require(isinstance(replay_error, Mapping), "replayed request did not fail")
    _require(
        replay_error.get("code")
        == int(PaymentErrorCode.PAYMENT_VERIFICATION_FAILED),
        "replayed request did not return payment-verification-failed",
    )
    _require(application_calls == 1, "application handler ran more than once")
    _require(processor.challenge_calls == 2, "unexpected challenge hook count")
    _require(processor.validation_calls == 2, "unexpected validation hook count")


def verify_mcp_transport_fixture(document: Any) -> None:
    fixture = load_json_rpc_mcp_fixture(document)
    asyncio.run(_verify_fixture_round_trip(fixture))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", type=Path)
    args = parser.parse_args()
    document = json.loads(args.fixtures.read_text(encoding="utf-8"))
    verify_mcp_transport_fixture(document)
    print(f"verified xrpl-mpp-mcp/{EXPECTED_FLOW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
