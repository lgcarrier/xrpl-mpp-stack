from __future__ import annotations

import httpx
import pytest

from xrpl_mpp_core import (
    PaymentCredential,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_content_digest,
    build_payment_challenge,
    decode_challenge_request,
    decode_payment_receipt,
    encode_payment_credential,
    encode_payment_receipt,
    extract_payment_challenges,
    jcs_dumps,
    parse_payment_authorization_header,
    parse_payment_challenge,
    render_payment_challenge,
)

SECRET = "mpp-http-test-secret"
RECIPIENT = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


def test_jcs_dumps_sorts_object_keys() -> None:
    assert jcs_dumps({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_build_content_digest_returns_rfc_9530_style_value() -> None:
    assert build_content_digest(b"hello world").startswith("sha-256=:")


def test_extract_payment_challenges_reads_repeated_www_authenticate_headers() -> None:
    charge = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    session = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP",
            channelId="C" * 64,
            recipient=RECIPIENT,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                cumulativeAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )

    headers = httpx.Headers(
        [
            ("WWW-Authenticate", render_payment_challenge(charge)),
            ("WWW-Authenticate", render_payment_challenge(session)),
        ]
    )
    decoded = extract_payment_challenges(headers)

    assert [item.intent for item in decoded] == ["charge", "session"]
    assert decode_challenge_request(decoded[1]).channel_id == "C" * 64


def test_extract_payment_challenges_reads_coalesced_www_authenticate_headers() -> None:
    primary = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    secondary = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="2000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="B" * 64),
        ),
        expires_in_seconds=300,
    )

    headers = httpx.Headers(
        {
            "WWW-Authenticate": (
                f"{render_payment_challenge(primary)}, {render_payment_challenge(secondary)}"
            )
        }
    )

    decoded = extract_payment_challenges(headers)

    assert [decode_challenge_request(item).amount for item in decoded] == ["1000", "2000"]


def test_extract_payment_challenges_accepts_mixed_case_payment_scheme() -> None:
    challenge = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )

    headers = httpx.Headers(
        {
            "WWW-Authenticate": render_payment_challenge(challenge).replace(
                "Payment ",
                "pAyMeNt ",
                1,
            )
        }
    )

    decoded = extract_payment_challenges(headers)

    assert len(decoded) == 1
    assert decoded[0] == challenge


def test_payment_challenge_parser_round_trips_escaped_description() -> None:
    challenge = build_payment_challenge(
        secret=SECRET,
        realm='merchant "east"',
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
        description='premium "gold" access',
    )

    parsed = parse_payment_challenge(render_payment_challenge(challenge))

    assert parsed.realm == 'merchant "east"'
    assert parsed.description == 'premium "gold" access'


def test_parse_payment_authorization_header_accepts_mixed_case_payment_scheme() -> None:
    challenge = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "transaction", "blob": "DEADBEEF"},
    )

    parsed = parse_payment_authorization_header(
        f"pAyMeNt {encode_payment_credential(credential)}"
    )

    assert parsed == credential


def test_render_payment_challenge_keeps_canonical_scheme_casing() -> None:
    challenge = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=RECIPIENT,
            methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )

    assert render_payment_challenge(challenge).startswith("Payment ")


def test_payment_receipt_round_trips_via_base64url_header() -> None:
    receipt = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123",
        challengeId="challenge-id",
        network="testnet",
        payer="rBuyer",
        recipient="rMerchant",
        invoiceId="A" * 64,
        txHash="ABC123",
        settlementStatus="validated",
    )

    encoded = encode_payment_receipt(receipt)
    decoded = decode_payment_receipt(encoded)

    assert decoded == receipt


def test_session_request_rejects_legacy_prepaid_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        XRPLSessionRequest(
            amount="250",
            currency="XRP",
            channelId="C" * 64,
            recipient=RECIPIENT,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                sessionId="session-123",
            ),
        )
