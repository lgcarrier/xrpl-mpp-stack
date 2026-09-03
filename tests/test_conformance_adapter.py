from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER = REPO_ROOT / "conformance" / "mpp-tools" / "xrpl-python" / "adapter.py"


def _call(operation: str, input_value: Any) -> dict[str, Any]:
    request = {"schema": 1, "op": operation, "input": input_value}
    result = subprocess.run(
        [sys.executable, str(ADAPTER)],
        input=json.dumps(request),
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        text=True,
        timeout=10,
    )
    assert result.stderr == ""
    return json.loads(result.stdout)


def _challenge() -> dict[str, Any]:
    return {
        "id": "ch_adapter_123",
        "realm": "api.example.com",
        "method": "tempo",
        "intent": "charge",
        "request": {"amount": "1000000"},
        "expires": "2099-01-01T00:00:00Z",
    }


def test_challenge_parse_and_format_use_the_canonical_adapter_shape() -> None:
    challenge = _challenge()
    formatted = _call("challenge.format", challenge)

    assert formatted["ok"] is True
    parsed = _call("challenge.parse", formatted["value"])
    assert parsed == {"ok": True, "value": challenge}


def test_credential_parse_and_format_decode_the_embedded_request() -> None:
    credential = {
        "challenge": _challenge(),
        "payload": {"type": "transaction", "signature": "0xabc123"},
        "source": "did:example:payer",
    }
    formatted = _call("credential.format", credential)

    assert formatted["ok"] is True
    parsed = _call("credential.parse", formatted["value"])
    assert parsed == {"ok": True, "value": credential}


def test_receipt_parse_and_format_round_trip() -> None:
    receipt = {
        "status": "success",
        "method": "tempo",
        "timestamp": "2026-01-29T12:00:30Z",
        "reference": "ref-adapter",
    }
    formatted = _call("receipt.format", receipt)

    assert formatted["ok"] is True
    parsed = _call("receipt.parse", formatted["value"])
    assert parsed == {"ok": True, "value": receipt}


def test_base64url_text_operations_cover_empty_utf8_and_invalid_bytes() -> None:
    assert _call("base64url.encode", {"text": "Hello, World!"}) == {
        "ok": True,
        "value": {"text": "SGVsbG8sIFdvcmxkIQ"},
    }
    assert _call("base64url.decode", {"text": "SGVsbG8sIFdvcmxkIQ"}) == {
        "ok": True,
        "value": {"text": "Hello, World!"},
    }
    assert _call("base64url.decode", {"text": ""}) == {
        "ok": True,
        "value": {"text": ""},
    }
    invalid = _call("base64url.decode", {"text": "_w"})
    assert invalid["ok"] is False
    assert invalid["error"]["type"] == "encoding_error"


def test_challenge_id_matches_the_mpp_tools_required_fields_vector() -> None:
    result = _call(
        "challenge.id",
        {
            "secretKey": "test-vector-secret-minimum-32-byte-secret",
            "realm": "api.example.com",
            "method": "tempo",
            "intent": "charge",
            "request": {"amount": "1000000"},
        },
    )

    assert result == {
        "ok": True,
        "value": {"id": "6QDs0BmhrI_v67iWeCMvGqQJEFQ6ErWKlk7zi5xL6t0"},
    }


def test_alternate_credential_header_survives_adapter_and_binds_challenge_id() -> None:
    challenge = {**_challenge(), "header": "Payment-Authorization"}

    formatted = _call("challenge.format", challenge)
    assert formatted["ok"] is True
    assert _call("challenge.parse", formatted["value"]) == {
        "ok": True,
        "value": challenge,
    }

    generated = _call(
        "challenge.id",
        {
            "secretKey": "test-vector-secret-minimum-32-byte-secret",
            "realm": "api.example.com",
            "method": "tempo",
            "intent": "charge",
            "request": {"amount": "1000000"},
            "header": "Payment-Authorization",
        },
    )
    assert generated == {
        "ok": True,
        "value": {"id": "vIwiTaU-7OGVC7LGBYFtgQZMo4d0qIvILb62cBlJiyU"},
    }


def test_adapter_maps_failures_to_operation_specific_error_types() -> None:
    cases = [
        ("challenge.parse", {"header": "not payment"}, "parse_error"),
        ("challenge.format", {}, "format_error"),
        ("credential.parse", {"header": "Bearer token"}, "parse_error"),
        ("credential.format", {}, "format_error"),
        ("receipt.parse", {"header": "not-valid!!!"}, "parse_error"),
        ("receipt.format", {}, "format_error"),
        ("base64url.encode", {"text": 123}, "encoding_error"),
        ("base64url.decode", {"text": "not-valid!!!"}, "encoding_error"),
        (
            "challenge.id",
            {
                "secretKey": "short",
                "realm": "api.example.com",
                "method": "tempo",
                "intent": "charge",
                "request": {},
            },
            "generation_error",
        ),
    ]

    for operation, input_value, expected_type in cases:
        response = _call(operation, input_value)
        assert response["ok"] is False
        assert response["error"]["type"] == expected_type


def test_adapter_reports_unsupported_operations_without_crashing() -> None:
    response = _call("server.verify", {})

    assert response["ok"] is False
    assert response["error"]["type"] == "unsupported_operation"
