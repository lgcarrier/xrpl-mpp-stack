from xrpl_mpp_middleware import (
    PaymentMiddlewareASGI,
    RouteConfig,
    XRPLAmount,
    XRPLAsset,
    XRPLFacilitatorClient,
    require_payment,
    require_session,
)
from xrpl_mpp_middleware.exceptions import RouteConfigurationError
import pytest


def test_package_exports_public_api() -> None:
    assert PaymentMiddlewareASGI is not None
    assert RouteConfig is not None
    assert XRPLAmount is not None
    assert XRPLAsset is not None
    assert XRPLFacilitatorClient is not None
    assert require_payment is not None
    assert require_session is not None


def test_require_payment_builds_charge_route_config() -> None:
    route_config = require_payment(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to="rDESTINATION123456789",
        network="xrpl:1",
        xrp_drops=1000,
        description="One paid request",
    )

    assert route_config.facilitator_url == "https://facilitator.example"
    assert route_config.charge_options[0].asset_identifier == "XRP:native"
    assert route_config.charge_options[0].amount == "1000"
    assert route_config.description == "One paid request"


def test_require_payment_rejects_missing_issued_asset_issuer() -> None:
    with pytest.raises(RouteConfigurationError, match="asset_issuer"):
        require_payment(
            facilitator_url="https://facilitator.example",
            bearer_token="secret-token",
            pay_to="rDESTINATION123456789",
            network="xrpl:1",
            amount="1.25",
            asset_code="RLUSD",
        )


def test_route_config_normalizes_issued_asset_identifiers() -> None:
    route_config = RouteConfig(
        facilitatorUrl="https://facilitator.example",
        bearerToken="secret-token",
        chargeOptions=[
            {
                "network": "xrpl:1",
                "recipient": "rDESTINATION123456789",
                "assetIdentifier": "rlusd:rIssuer",
                "amount": "1.25",
            }
        ],
        sessionOptions=[
            {
                "network": "xrpl:1",
                "recipient": "rDESTINATION123456789",
                "assetIdentifier": "usdc:rIssuer",
                "amount": "2.5",
                "minPrepayAmount": "10",
            }
        ],
    )

    assert route_config.charge_options[0].asset_identifier == "RLUSD:rIssuer"
    assert route_config.session_options[0].asset_identifier == "USDC:rIssuer"


def test_require_session_builds_session_route_config() -> None:
    route_config = require_session(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to="rDESTINATION123456789",
        network="xrpl:1",
        xrp_drops=250,
        min_prepay_amount="1000",
        idle_timeout_seconds=600,
        description="Metered session route",
    )

    assert route_config.session_options[0].asset_identifier == "XRP:native"
    assert route_config.session_options[0].amount == "250"
    assert route_config.session_options[0].min_prepay_amount == "1000"
    assert route_config.session_options[0].unit_amount == "250"
    assert route_config.session_options[0].idle_timeout_seconds == 600


def test_require_session_normalizes_issued_asset_identifier() -> None:
    route_config = require_session(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to="rDESTINATION123456789",
        network="xrpl:1",
        amount="1.25",
        min_prepay_amount="5",
        asset_code="rlusd",
        asset_issuer="rIssuer",
    )

    assert route_config.session_options[0].asset_identifier == "RLUSD:rIssuer"


def test_require_session_rejects_divergent_unit_amount() -> None:
    with pytest.raises(ValueError, match="unitAmount must match amount"):
        require_session(
            facilitator_url="https://facilitator.example",
            bearer_token="secret-token",
            pay_to="rDESTINATION123456789",
            network="xrpl:1",
            xrp_drops=250,
            min_prepay_amount="1000",
            unit_amount="500",
        )


def test_facilitator_settings_allow_blank_allowed_issued_assets_placeholder() -> None:
    from xrpl_mpp_facilitator.config import Settings

    settings = Settings(
        MY_DESTINATION_ADDRESS="rDESTINATION123456789",
        FACILITATOR_BEARER_TOKEN="secret-token",
        REDIS_URL="redis://localhost:6379/0",
        MPP_CHALLENGE_SECRET="challenge-secret",
        ALLOWED_ISSUED_ASSETS="   # optional extra CODE:ISSUER pairs",
    )

    assert settings.ALLOWED_ISSUED_ASSETS == ""
