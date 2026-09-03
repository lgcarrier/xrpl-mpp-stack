from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from xrpl_mpp_mcp import (
    CREDENTIAL_META_KEY,
    RECEIPT_META_KEY,
    CallbackPaymentProcessor,
    ConflictingPaymentMetadataError,
    MCPPaymentChallenge,
    MCPPaymentCredential,
    MCPPaymentReceipt,
    PaidOperationBinding,
    PaymentErrorCode,
    PaymentVerificationFailed,
    build_bound_challenge,
    build_operation_binding,
    build_payment_capabilities,
    canonical_json,
    challenge_is_expired,
    extract_payment_capabilities,
    extract_paid_operation_credential,
    extract_payment_credential,
    extract_payment_receipt,
    paid_operation,
    payment_required_response,
    should_drop_paid_notification,
    verify_bound_challenge,
    with_payment_capabilities,
    with_payment_credential,
    with_payment_receipt,
)


def _message(*, request_id: int | None = 1, query: str = "mpp") -> dict[str, Any]:
    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {"query": query},
            "_meta": {"example/client": "kept"},
        },
    }
    if request_id is not None:
        message["id"] = request_id
    return message


def _challenge(challenge_id: str = "challenge-1") -> MCPPaymentChallenge:
    return MCPPaymentChallenge(
        id=challenge_id,
        realm="tools.example",
        method="xrpl",
        intent="charge",
        request={
            "amount": "1000",
            "currency": "XRP",
            "recipient": "rMerchant",
        },
        expires="2099-01-01T00:00:00Z",
        futureExtension={"kept": True},
    )


def _credential(challenge: MCPPaymentChallenge | None = None) -> MCPPaymentCredential:
    return MCPPaymentCredential(
        challenge=challenge or _challenge(),
        source="did:xrpl:testnet:rPayer",
        payload={"type": "hash", "hash": "ABCDEF"},
    )


def _receipt(challenge_id: str = "challenge-1") -> MCPPaymentReceipt:
    return MCPPaymentReceipt(
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        challengeId=challenge_id,
        reference="ABCDEF",
        ledgerIndex=123,
    )


def test_native_json_models_retain_extension_fields() -> None:
    challenge = _challenge()
    dumped = challenge.model_dump(by_alias=True, exclude_none=True)

    assert isinstance(dumped["request"], dict)
    assert dumped["request"]["amount"] == "1000"
    assert dumped["futureExtension"] == {"kept": True}


def test_payment_capability_round_trip_preserves_other_capabilities() -> None:
    payment = build_payment_capabilities({"xrpl": ["charge", "session"]})
    original = {"capabilities": {"tools": {}, "experimental": {"other": True}}}

    advertised = with_payment_capabilities(original, payment)

    assert original == {"capabilities": {"tools": {}, "experimental": {"other": True}}}
    assert advertised["capabilities"]["experimental"]["other"] is True
    assert extract_payment_capabilities(advertised) == payment


def test_metadata_supports_root_and_nested_placement() -> None:
    credential = _credential()
    root = with_payment_credential(_message(), credential, placement="root")
    nested = with_payment_credential(_message(), credential, placement="nested")

    assert extract_payment_credential(root) == credential
    assert extract_payment_credential(nested) == credential
    assert nested["params"]["_meta"]["example/client"] == "kept"
    assert CREDENTIAL_META_KEY in nested["params"]["_meta"]


def test_payment_metadata_is_ignored_on_unpaid_methods() -> None:
    message = with_payment_credential(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {},
        },
        _credential(),
    )

    assert extract_payment_credential(message) == _credential()
    assert extract_paid_operation_credential(message) is None


def test_conflicting_root_and_nested_metadata_is_rejected() -> None:
    message = with_payment_credential(_message(), _credential(), placement="root")
    message = with_payment_credential(
        message,
        _credential(_challenge("different")),
        placement="nested",
    )

    with pytest.raises(ConflictingPaymentMetadataError):
        extract_payment_credential(message)


def test_receipt_supports_root_and_nested_placement() -> None:
    response = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    nested = with_payment_receipt(response, _receipt())
    root = with_payment_receipt(response, _receipt(), placement="root")

    assert extract_payment_receipt(nested) == _receipt()
    assert extract_payment_receipt(root) == _receipt()
    assert RECEIPT_META_KEY in nested["result"]["_meta"]


@pytest.mark.parametrize(
    ("method", "params", "target"),
    [
        ("tools/call", {"name": "search", "arguments": {"q": "x"}}, "search"),
        ("resources/read", {"uri": "data://premium"}, "data://premium"),
        ("prompts/get", {"name": "expert", "arguments": {}}, "expert"),
    ],
)
def test_paid_operation_binding_covers_supported_operations(
    method: str,
    params: dict[str, Any],
    target: str,
) -> None:
    binding = build_operation_binding(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )

    assert binding.method == method
    assert binding.target == target
    assert binding.digest


def test_operation_binding_ignores_request_id_and_payment_credential_only() -> None:
    first = _message(request_id=1)
    second = with_payment_credential(_message(request_id=999), _credential())

    assert build_operation_binding(first).digest == build_operation_binding(second).digest
    assert build_operation_binding(_message(query="different")).digest != build_operation_binding(
        first
    ).digest


def test_canonical_json_uses_rfc8785_number_and_utf16_ordering() -> None:
    rendered = canonical_json(
        {
            "\ue000": "private-use",
            "😀": "supplementary",
            "numbers": [-0.0, 1e-7, 1e-6, 1e20, 1e21],
        }
    )

    assert rendered == (
        '{"numbers":[0,1e-7,0.000001,100000000000000000000,1e+21],'
        '"😀":"supplementary","\ue000":"private-use"}'
    )

    with pytest.raises(ValueError, match="safe range"):
        canonical_json({"unsafe": 9_007_199_254_740_992})


def test_bound_challenge_covers_operation_and_supports_secret_rotation() -> None:
    binding = build_operation_binding(_message())
    challenge = build_bound_challenge(
        secret="active-secret",
        realm="tools.example",
        payment_method="xrpl",
        intent="charge",
        request={"currency": "XRP", "amount": "1000"},
        operation=binding,
        expires="2099-01-01T00:00:00Z",
    )

    assert verify_bound_challenge(
        challenge,
        binding,
        secrets=["previous-secret", "active-secret"],
    )
    assert not verify_bound_challenge(
        challenge,
        build_operation_binding(_message(query="other")),
        secrets="active-secret",
    )
    assert challenge_is_expired(challenge, now=datetime(2100, 1, 1, tzinfo=UTC))


def test_error_codes_are_exact_and_challenges_remain_native_json() -> None:
    response = payment_required_response(7, [_challenge()])

    assert int(PaymentErrorCode.PAYMENT_REQUIRED) == -32042
    assert int(PaymentErrorCode.PAYMENT_VERIFICATION_FAILED) == -32043
    assert int(PaymentErrorCode.INVALID_PARAMS) == -32602
    assert int(PaymentErrorCode.INTERNAL_ERROR) == -32603
    assert response["error"]["code"] == -32042
    assert response["error"]["data"]["challenges"][0]["request"]["amount"] == "1000"


def test_paid_notifications_are_dropped() -> None:
    assert should_drop_paid_notification(_message(request_id=None))
    assert not should_drop_paid_notification(_message(request_id=1))
    assert not should_drop_paid_notification(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )


class FakeProcessor:
    def __init__(self) -> None:
        self.challenge_count = 0
        self.validation_count = 0
        self.fail_validation = False

    async def create_challenges(
        self,
        operation: PaidOperationBinding,
    ) -> Sequence[MCPPaymentChallenge]:
        self.challenge_count += 1
        return [_challenge(f"fresh-{self.challenge_count}")]

    async def validate_and_consume(
        self,
        credential: MCPPaymentCredential,
        operation: PaidOperationBinding,
    ) -> MCPPaymentReceipt:
        self.validation_count += 1
        if self.fail_validation:
            raise PaymentVerificationFailed("proof-invalid", "invalid XRPL proof")
        return _receipt(credential.challenge.id)


def test_decorator_challenges_before_invoking_handler() -> None:
    processor = FakeProcessor()
    handler_calls = 0

    @paid_operation(processor, expected_method="tools/call")
    async def handler(context: Any) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"content": []}

    response = asyncio.run(handler(_message()))

    assert response is not None
    assert response["error"]["code"] == -32042
    assert handler_calls == 0
    assert processor.validation_count == 0


def test_decorator_validates_then_injects_receipt_into_success() -> None:
    processor = FakeProcessor()
    seen_params: dict[str, Any] | None = None

    @paid_operation(processor, expected_method="tools/call")
    async def handler(context: Any) -> dict[str, Any]:
        nonlocal seen_params
        seen_params = dict(context.params)
        return {"content": [{"type": "text", "text": "paid"}]}

    request = with_payment_credential(_message(), _credential())
    response = asyncio.run(handler(request))

    assert response is not None
    assert response["result"]["content"][0]["text"] == "paid"
    assert response["result"]["_meta"][RECEIPT_META_KEY]["challengeId"] == "challenge-1"
    assert CREDENTIAL_META_KEY not in seen_params.get("_meta", {})
    assert seen_params["_meta"]["example/client"] == "kept"
    assert processor.validation_count == 1


def test_decorator_returns_fresh_verification_failure_without_handler() -> None:
    processor = FakeProcessor()
    processor.fail_validation = True
    handler_calls = 0

    @paid_operation(processor)
    async def handler(context: Any) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"content": []}

    response = asyncio.run(handler(with_payment_credential(_message(), _credential())))

    assert response is not None
    assert response["error"]["code"] == -32043
    assert response["error"]["data"]["challenges"][0]["id"] == "fresh-1"
    assert response["error"]["data"]["failure"]["reason"] == "proof-invalid"
    assert handler_calls == 0


def test_decorator_maps_malformed_credential_to_invalid_params() -> None:
    processor = FakeProcessor()

    @paid_operation(processor)
    async def handler(context: Any) -> dict[str, Any]:
        raise AssertionError("handler must not run")

    message = _message()
    message["params"]["_meta"][CREDENTIAL_META_KEY] = {"payload": {}}
    response = asyncio.run(handler(message))

    assert response is not None
    assert response["error"]["code"] == -32602
    assert processor.validation_count == 0


def test_decorator_never_attaches_receipt_to_application_error() -> None:
    processor = FakeProcessor()

    @paid_operation(processor)
    async def handler(context: Any) -> dict[str, Any]:
        raise RuntimeError("application failed after settlement")

    response = asyncio.run(handler(with_payment_credential(_message(), _credential())))

    assert response is not None
    assert response["error"]["code"] == -32603
    assert RECEIPT_META_KEY not in response.get("_meta", {})
    assert "result" not in response


def test_decorator_drops_notification_before_processor_and_handler() -> None:
    processor = FakeProcessor()
    handler_calls = 0

    @paid_operation(processor)
    async def handler(context: Any) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"content": []}

    response = asyncio.run(handler(_message(request_id=None)))

    assert response is None
    assert processor.challenge_count == 0
    assert processor.validation_count == 0
    assert handler_calls == 0


def test_callback_processor_accepts_sync_replay_safe_hooks() -> None:
    operation = build_operation_binding(_message())
    credential = _credential()
    processor = CallbackPaymentProcessor(
        challenge_hook=lambda _: [_challenge()],
        validation_hook=lambda submitted, _: _receipt(submitted.challenge.id),
    )

    assert asyncio.run(processor.create_challenges(operation)) == [_challenge()]
    assert asyncio.run(processor.validate_and_consume(credential, operation)) == _receipt()
