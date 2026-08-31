from __future__ import annotations

import pytest

from xrpl_mpp_core import IssuedCurrency, serialize_currency
from xrpl_mpp_middleware import (
    HookDispatcher,
    PaymentMiddlewareASGI,
    PaymentOffer,
    PaymentOutcomeRelay,
    RouteConfig,
    XRPLFacilitatorClient,
    augment_openapi,
    require_payment,
    require_session,
)
from xrpl_mpp_middleware.exceptions import RouteConfigurationError


RECIPIENT = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
ISSUER = "rPEPPER7kfTD9w2To4CQk6UCfuHM9c6GDY"


def test_package_exports_public_v02_api() -> None:
    assert PaymentMiddlewareASGI is not None
    assert RouteConfig is not None
    assert XRPLFacilitatorClient is not None
    assert HookDispatcher is not None
    assert PaymentOutcomeRelay is not None
    assert PaymentOffer is not None
    assert augment_openapi is not None
    assert require_payment is not None
    assert require_session is not None


def test_require_payment_builds_named_network_xrp_terms() -> None:
    route_config = require_payment(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to=RECIPIENT,
        network="testnet",
        xrp_drops=1000,
        description="One paid request",
    )

    option = route_config.charge_options[0]
    assert option.network == "testnet"
    assert option.currency == "XRP"
    assert option.amount == "1000"
    assert route_config.description == "One paid request"


def test_require_payment_builds_json_issued_currency() -> None:
    route_config = require_payment(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to=RECIPIENT,
        network="mainnet",
        amount="1.25",
        asset_code="RLUSD",
        asset_issuer=ISSUER,
    )

    assert route_config.charge_options[0].currency == serialize_currency(
        IssuedCurrency(currency="524C555344000000000000000000000000000000", issuer=ISSUER)
    )


def test_require_payment_rejects_missing_issued_asset_issuer() -> None:
    with pytest.raises(RouteConfigurationError, match="asset_issuer"):
        require_payment(
            facilitator_url="https://facilitator.example",
            bearer_token="secret-token",
            pay_to=RECIPIENT,
            network="testnet",
            amount="1.25",
            asset_code="RLUSD",
        )


def test_require_session_builds_xrp_paychannel_terms() -> None:
    route_config = require_session(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to=RECIPIENT,
        network="testnet",
        xrp_drops=250,
        channel_id="A" * 64,
        description="Metered PayChannel route",
    )

    option = route_config.session_options[0]
    assert option.network == "testnet"
    assert option.currency == "XRP"
    assert option.amount == "250"
    assert option.channel_id == "A" * 64


def test_route_config_rejects_legacy_asset_identifier_shape() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        RouteConfig(
            facilitatorUrl="https://facilitator.example",
            bearerToken="secret-token",
            chargeOptions=[
                {
                    "network": "testnet",
                    "recipient": RECIPIENT,
                    "assetIdentifier": "XRP:native",
                    "amount": "1000",
                }
            ],
        )


def test_facilitator_settings_allow_blank_issued_asset_placeholder() -> None:
    from xrpl_mpp_facilitator.config import Settings

    settings = Settings(
        _env_file=None,
        MY_DESTINATION_ADDRESS=RECIPIENT,
        FACILITATOR_BEARER_TOKEN="secret-token",
        REDIS_URL="redis://localhost:6379/0",
        MPP_CHALLENGE_SECRET="challenge-secret",
        ALLOWED_ISSUED_ASSETS="   # optional extra CODE:ISSUER pairs",
    )

    assert settings.ALLOWED_ISSUED_ASSETS == ""
