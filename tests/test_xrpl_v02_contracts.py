from __future__ import annotations

import pytest
from pydantic import ValidationError

from xrpl_mpp_core.binding import (
    challenge_invoice_id,
    is_invoice_id,
    normalize_invoice_id,
)
from xrpl_mpp_core.did import build_xrpl_did, classic_address_from_did, parse_xrpl_did
from xrpl_mpp_core.paychannel import (
    PayChannelCumulativeError,
    PayChannelHighWater,
    XRPLChannelClosePayload,
    XRPLChannelOpenPayload,
    XRPLChannelVoucherPayload,
    XRPLSessionRequest,
    evaluate_high_water,
    require_high_water_advance,
    validate_session_payload,
)
from xrpl_mpp_core.xrpl import (
    IssuedCurrency,
    MPToken,
    XRP,
    XRPLChargeRequest,
    XRPLHashCredentialPayload,
    XRPLTransactionCredentialPayload,
    build_ledger_amount,
    parse_currency,
    serialize_currency,
    validate_charge_payload,
)


PAYER = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RECIPIENT = "rf5kMNrUqgLzJT8YUzxM1pptc5r3Lfx1J9"
CHANNEL_ID = "AB" * 32
SIGNATURE = "CD" * 64
TX_HASH = "EF" * 32
MPT_ID = "00000001A407AF5856CEFB379FAE300376E06FCEEDDC455BE0"


def test_currency_wire_encodings_match_ripple_sdk() -> None:
    assert parse_currency("XRP") == XRP

    issued = IssuedCurrency(currency="USD", issuer=PAYER)
    issued_wire = f'{{"currency":"USD","issuer":"{PAYER}"}}'
    assert serialize_currency(issued) == issued_wire
    assert parse_currency(issued_wire) == issued
    assert build_ledger_amount("10.25", issued) == {
        "currency": "USD",
        "issuer": PAYER,
        "value": "10.25",
    }

    mpt = MPToken(mpt_issuance_id=MPT_ID)
    assert serialize_currency(mpt) == f'{{"mpt_issuance_id":"{MPT_ID}"}}'
    assert parse_currency(serialize_currency(mpt)) == mpt
    assert build_ledger_amount("100", mpt) == {
        "mpt_issuance_id": MPT_ID,
        "value": "100",
    }
    assert build_ledger_amount("1000000", XRP) == "1000000"


@pytest.mark.parametrize(
    "wire",
    [
        f"USD:{PAYER}",
        f'{{"currency":"USD","issuer":"{PAYER}","extra":true}}',
        f'{{"currency":"USD","currency":"EUR","issuer":"{PAYER}"}}',
        "[]",
    ],
)
def test_currency_parser_rejects_noncanonical_or_ambiguous_values(wire: str) -> None:
    with pytest.raises(ValueError):
        parse_currency(wire)


def test_charge_request_and_payloads_match_ripple_sdk_aliases() -> None:
    request = XRPLChargeRequest.model_validate(
        {
            "amount": "1000000",
            "currency": "XRP",
            "recipient": RECIPIENT,
            "description": "weather",
            "externalId": "order-1",
            "methodDetails": {
                "reference": "ref-1",
                "network": "testnet",
                "invoiceId": "01" * 32,
                "destinationTag": 7,
                "sourceTag": 593184257,
                "memos": [{"type": "text/plain", "data": "order-1"}],
            },
        }
    )
    dumped = request.model_dump(by_alias=True, exclude_none=True)
    assert dumped["externalId"] == "order-1"
    assert dumped["methodDetails"]["invoiceId"] == "01" * 32
    assert dumped["methodDetails"]["destinationTag"] == 7

    pull = validate_charge_payload({"type": "transaction", "blob": "12000022"})
    push = validate_charge_payload({"type": "hash", "hash": TX_HASH})
    assert isinstance(pull, XRPLTransactionCredentialPayload)
    assert isinstance(push, XRPLHashCredentialPayload)

    with pytest.raises(ValidationError):
        XRPLChargeRequest.model_validate({**dumped, "unexpected": True})
    with pytest.raises(ValidationError):
        validate_charge_payload({"type": "hash", "hash": "not-a-hash"})


def test_invoice_id_binding_matches_ripple_sha512half_vector() -> None:
    expected = "F7CABCF585AD599C4380C15B48E55B35FFB60CB46F7B904E7214F0443D481DFC"
    assert challenge_invoice_id("challenge-001") == expected
    assert is_invoice_id(expected)
    assert normalize_invoice_id(expected.lower()) == expected
    with pytest.raises(ValueError, match="must not be empty"):
        challenge_invoice_id("")


def test_xrpl_did_round_trip_and_network_binding() -> None:
    source = build_xrpl_did(network="testnet", address=PAYER)
    assert source == f"did:pkh:xrpl:testnet:{PAYER}"
    parsed = parse_xrpl_did(source, expected_network="testnet")
    assert parsed.network == "testnet"
    assert parsed.address == PAYER
    assert classic_address_from_did(source) == PAYER

    with pytest.raises(ValueError, match="does not match"):
        parse_xrpl_did(source, expected_network="mainnet")
    with pytest.raises((ValueError, ValidationError)):
        parse_xrpl_did(f"did:pkh:xrpl:xrpl:1:{PAYER}")


def test_session_request_and_all_paychannel_actions_match_ripple_sdk() -> None:
    request = XRPLSessionRequest.model_validate(
        {
            "amount": "50000",
            "currency": "XRP",
            "channelId": CHANNEL_ID,
            "recipient": RECIPIENT,
            "externalId": "session-1",
            "methodDetails": {
                "reference": "ref-2",
                "network": "testnet",
                "cumulativeAmount": "200000",
            },
        }
    )
    assert request.model_dump(by_alias=True, exclude_none=True)["channelId"] == CHANNEL_ID

    open_payload = validate_session_payload(
        {
            "action": "open",
            "transaction": "12000022",
            "amount": "0",
            "signature": SIGNATURE,
        }
    )
    voucher_payload = validate_session_payload(
        {
            "action": "voucher",
            "channelId": CHANNEL_ID,
            "amount": "250000",
            "signature": SIGNATURE,
        }
    )
    close_payload = validate_session_payload(
        {
            "action": "close",
            "channelId": CHANNEL_ID,
            "amount": "250000",
            "signature": SIGNATURE,
        }
    )
    assert isinstance(open_payload, XRPLChannelOpenPayload)
    assert isinstance(voucher_payload, XRPLChannelVoucherPayload)
    assert isinstance(close_payload, XRPLChannelClosePayload)

    opening_request = XRPLSessionRequest(
        amount="0",
        currency="XRP",
        channelId="",
        recipient=RECIPIENT,
    )
    assert opening_request.channel_id == ""


def test_high_water_decisions_match_atomic_reference_semantics() -> None:
    initial = evaluate_high_water(
        None,
        cumulative="100",
        requested="100",
        signature=SIGNATURE,
        timestamp=1,
    )
    assert initial.status == "advanced"
    assert initial.previous == "0"
    assert initial.state == PayChannelHighWater(
        cumulative="100",
        signature=SIGNATURE,
        timestamp=1,
    )

    assert evaluate_high_water(
        initial.state,
        cumulative="100",
        requested="1",
        signature=SIGNATURE,
    ).status == "replay"
    assert evaluate_high_water(
        initial.state,
        cumulative="99",
        requested="1",
        signature=SIGNATURE,
    ).status == "regressed"
    assert evaluate_high_water(
        initial.state,
        cumulative="149",
        requested="50",
        signature=SIGNATURE,
    ).status == "short"

    advanced = require_high_water_advance(
        initial.state,
        cumulative="150",
        requested="50",
        signature="EF" * 64,
        timestamp=2,
    )
    assert advanced.cumulative == "150"
    assert advanced.timestamp == 2

    with pytest.raises(PayChannelCumulativeError) as caught:
        require_high_water_advance(
            advanced,
            cumulative="150",
            requested="1",
            signature=SIGNATURE,
        )
    assert caught.value.status == "replay"


def test_high_water_allows_zero_open_only_without_a_claim_signature() -> None:
    zero = PayChannelHighWater(cumulative="0", signature="", timestamp=0)
    assert zero.cumulative == "0"
    with pytest.raises(ValidationError, match="requires a claim signature"):
        PayChannelHighWater(cumulative="1", signature="", timestamp=0)
