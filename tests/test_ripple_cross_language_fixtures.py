from __future__ import annotations

import json
from pathlib import Path

from xrpl_mpp_core import (
    PaymentReceipt,
    XRPLChargeRequest,
    XRPLSessionRequest,
    challenge_invoice_id,
    validate_charge_payload,
    validate_session_payload,
)


RIPPLE_SDK_REF = "6907484c92d217da406e2f3d7b5e6587703c6ea8"
FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "conformance"
    / "ripple-xrpl-sdk"
    / "fixtures.json"
)


def _fixtures() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_ripple_sdk_charge_fixture_matches_python_contracts() -> None:
    fixtures = _fixtures()
    charge = fixtures["charge"]

    assert fixtures["source"]["commit"] == RIPPLE_SDK_REF
    assert charge["method"] == {"name": "xrpl", "intent": "charge"}
    request = XRPLChargeRequest.model_validate(charge["request"])
    assert request.method_details is not None
    assert request.method_details.invoice_id == "01" * 32
    assert validate_charge_payload(charge["payloads"]["transaction"]).type == "transaction"
    assert validate_charge_payload(charge["payloads"]["hash"]).type == "hash"

    receipt = PaymentReceipt.model_validate(charge["receipt"])
    assert receipt.reference == fixtures["constants"]["txHash"]
    assert receipt.model_extra == {
        "externalId": fixtures["invoiceId"]["challengeId"]
    }
    assert fixtures["invoiceId"]["value"] == challenge_invoice_id(
        fixtures["invoiceId"]["challengeId"]
    )


def test_ripple_sdk_session_fixture_matches_python_open_voucher_close_contracts() -> None:
    fixtures = _fixtures()
    session = fixtures["session"]
    channel_id = fixtures["constants"]["channelId"]
    tx_hash = fixtures["constants"]["txHash"]

    assert session["method"] == {"name": "xrpl", "intent": "session"}
    request = XRPLSessionRequest.model_validate(session["request"])
    open_request = XRPLSessionRequest.model_validate(session["openRequest"])
    assert request.channel_id == channel_id
    assert open_request.channel_id == ""
    assert open_request.amount == "0"

    assert validate_session_payload(session["payloads"]["open"]).action == "open"
    assert validate_session_payload(session["payloads"]["voucher"]).action == "voucher"
    assert validate_session_payload(session["payloads"]["close"]).action == "close"

    expected_references = {
        "open": f"open:{channel_id}:{tx_hash}",
        "voucher": f"{channel_id}:250000",
        "close": f"{channel_id}:250000",
    }
    expected_external_ids = {
        "open": "session-open-fixture-001",
        "voucher": "session-voucher-fixture-001",
        "close": "session-close-fixture-001",
    }
    for action, raw_receipt in session["receipts"].items():
        receipt = PaymentReceipt.model_validate(raw_receipt)
        assert receipt.method == "xrpl"
        assert receipt.reference == expected_references[action]
        assert receipt.model_extra == {
            "externalId": expected_external_ids[action]
        }
