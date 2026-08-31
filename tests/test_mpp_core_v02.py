from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from pydantic import ValidationError

from xrpl_mpp_core import (
    AUTHORIZATION_HEADER,
    PAYMENT_AUTHORIZATION_HEADER,
    AcceptPaymentRange,
    ChallengeKeyRing,
    PaymentChallenge,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    build_challenge_id,
    build_payment_challenge,
    encode_json_to_base64url,
    extract_payment_challenges,
    is_valid_xrpl_network,
    parse_accept_payment,
    parse_payment_challenge,
    payment_credential_header,
    rank_payment_challenges,
    render_accept_payment,
    render_payment_challenge,
    verify_challenge_binding,
)


SECRET = "active-secret"


def _request() -> XRPLChargeRequest:
    return XRPLChargeRequest(
        amount="10",
        currency="XRP",
        recipient="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
        methodDetails=XRPLChargeMethodDetails(network="testnet", invoiceId="A" * 64),
    )


def _challenge(*, intent: str = "charge", header: str | None = None) -> PaymentChallenge:
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent=intent,
        request_model=_request(),
        expires_in_seconds=60,
        header=header,
    )


@pytest.mark.parametrize("network", ["mainnet", "testnet", "devnet"])
def test_xrpl_network_helper_accepts_only_mpp_02_named_networks(network: str) -> None:
    assert is_valid_xrpl_network(network)


@pytest.mark.parametrize("network", ["xrpl:0", "xrpl:1", "xrpl:testnet", "Testnet", ""])
def test_xrpl_network_helper_rejects_pre_02_and_unknown_networks(network: str) -> None:
    assert not is_valid_xrpl_network(network)


def test_alternate_header_round_trips_and_is_bound_as_conditional_eighth_slot() -> None:
    challenge = _challenge(header=PAYMENT_AUTHORIZATION_HEADER)
    parsed = parse_payment_challenge(render_payment_challenge(challenge))

    slots = [
        challenge.realm,
        challenge.method,
        challenge.intent,
        challenge.request,
        challenge.expires or "",
        challenge.digest or "",
        challenge.opaque or "",
        PAYMENT_AUTHORIZATION_HEADER,
    ]
    expected = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), "|".join(slots).encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

    assert challenge.id == expected
    assert parsed == challenge
    assert payment_credential_header(parsed) == PAYMENT_AUTHORIZATION_HEADER
    assert verify_challenge_binding(parsed, secret=SECRET)


def test_headerless_challenge_keeps_the_seven_slot_binding() -> None:
    challenge = _challenge()

    assert challenge.header is None
    assert payment_credential_header(challenge) == AUTHORIZATION_HEADER
    assert challenge.id == build_challenge_id(
        secret=SECRET,
        realm=challenge.realm,
        method=challenge.method,
        intent=challenge.intent,
        request_b64=challenge.request,
        expires=challenge.expires,
        digest=challenge.digest,
        opaque=challenge.opaque,
    )


@pytest.mark.parametrize(
    ("opaque", "header", "expected"),
    [
        (None, None, "6QDs0BmhrI_v67iWeCMvGqQJEFQ6ErWKlk7zi5xL6t0"),
        (None, PAYMENT_AUTHORIZATION_HEADER, "vIwiTaU-7OGVC7LGBYFtgQZMo4d0qIvILb62cBlJiyU"),
        (
            "eyJyb3V0ZSI6Ii9hcGkvcHJlbWl1bSJ9",
            PAYMENT_AUTHORIZATION_HEADER,
            "dFqwY5awqN-2_6qSCqi5W2wrSmiq2y1VmYP-4W4a24k",
        ),
    ],
)
def test_normative_challenge_id_vectors_include_header_after_opaque(
    opaque: str | None,
    header: str | None,
    expected: str,
) -> None:
    assert build_challenge_id(
        secret="test-vector-secret-minimum-32-byte-secret",
        realm="api.example.com",
        method="tempo",
        intent="charge",
        request_b64="eyJhbW91bnQiOiIxMDAwMDAwIn0",
        opaque=opaque,
        header=header,
    ) == expected


def test_challenge_rejects_arbitrary_credential_header() -> None:
    with pytest.raises(ValidationError, match="Payment-Authorization"):
        PaymentChallenge(
            id="challenge",
            realm="merchant.example",
            method="xrpl",
            intent="charge",
            request=encode_json_to_base64url({"amount": "1"}),
            header="X-Payment",
        )


def test_challenge_key_ring_signs_with_active_and_verifies_previous() -> None:
    old = build_payment_challenge(
        secret="previous-secret",
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=_request(),
    )
    ring = ChallengeKeyRing([SECRET, "previous-secret"])

    assert ring.active == SECRET
    assert ring.verifies(old)
    assert verify_challenge_binding(old, secrets=ring.secrets)


def test_parser_rejects_duplicate_auth_params() -> None:
    challenge = _challenge()
    rendered = render_payment_challenge(challenge)

    with pytest.raises(ValueError, match="duplicate"):
        parse_payment_challenge(f'{rendered}, realm="other.example"')


def test_parser_accepts_rfc9110_token_auth_param_values() -> None:
    challenge = _challenge()
    rendered = (
        f"Payment id={challenge.id}, realm={challenge.realm}, method={challenge.method}, "
        f"intent={challenge.intent}, request={challenge.request}, "
        f'expires="{challenge.expires}"'
    )

    assert parse_payment_challenge(rendered) == challenge


def test_parser_preserves_auth_param_value_whitespace_exactly() -> None:
    parsed = parse_payment_challenge(
        'Payment id=" challenge ", realm=" merchant.example ", method="xrpl", '
        'intent="charge", request="e30"'
    )

    assert parsed.id == " challenge "
    assert parsed.realm == " merchant.example "


@pytest.mark.parametrize(
    "header_value",
    [
        lambda rendered: f'{rendered}, Basic realm="legacy"',
        lambda rendered: f'Basic realm="legacy", {rendered}',
    ],
)
def test_parser_extracts_payment_among_mixed_authentication_schemes(header_value) -> None:
    challenge = _challenge()
    rendered = render_payment_challenge(challenge)

    assert extract_payment_challenges(
        {"WWW-Authenticate": header_value(rendered)}
    ) == [challenge]


def test_challenge_rejects_padded_or_noncanonical_request() -> None:
    with pytest.raises(ValidationError, match="without padding"):
        PaymentChallenge(
            id="challenge",
            realm="merchant.example",
            method="xrpl",
            intent="charge",
            request="e30=",
        )


def test_challenge_allows_extensible_intent_and_ignores_unknown_extension() -> None:
    challenge = PaymentChallenge.model_validate(
        {
            "id": "challenge",
            "realm": "merchant.example",
            "method": "xrpl",
            "intent": "subscription",
            "request": encode_json_to_base64url({"amount": "1"}),
            "future-extension": "ignored",
        }
    )

    assert challenge.intent == "subscription"


def test_receipt_preserves_method_and_intent_extensions() -> None:
    receipt = PaymentReceipt.model_validate(
        {
            "status": "success",
            "method": "futurepay",
            "timestamp": "2026-08-30T12:00:00Z",
            "reference": "payment-123",
            "futureExtension": {"subscriptionId": "sub-123"},
        }
    )

    assert receipt.model_dump(by_alias=True)["futureExtension"] == {
        "subscriptionId": "sub-123"
    }


def test_accept_payment_parses_renders_and_ranks_by_specific_q() -> None:
    ranges = parse_accept_payment("xrpl/*;q=0.4, xrpl/charge;q=0.9, xrpl/session;q=0")
    charge = _challenge(intent="charge")
    session = _challenge(intent="session")

    assert render_accept_payment(ranges) == "xrpl/*;q=0.4, xrpl/charge;q=0.9, xrpl/session;q=0"
    assert rank_payment_challenges([session, charge], ranges) == [charge]


@pytest.mark.parametrize("raw", ["", "XRPL/charge", "xrpl", "xrpl/charge;q=1.1", "xrpl/charge;q=.5"])
def test_accept_payment_rejects_malformed_ranges(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_accept_payment(raw)


def test_accept_payment_range_rejects_more_than_three_q_decimals() -> None:
    with pytest.raises(ValidationError, match="three decimal"):
        AcceptPaymentRange(method="xrpl", intent="charge", q="0.1234")
