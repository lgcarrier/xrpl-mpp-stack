import pytest
from pathlib import Path

from xrpl_mpp_facilitator.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[1]


def compose_service_block(service: str) -> str:
    lines = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {service}:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("    ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def build_settings(**overrides: object) -> Settings:
    settings_data = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": "rTESTDESTINATIONADDRESS123456789",
        "NETWORK_ID": "testnet",
        "REDIS_URL": "redis://redis:6379/0",
        "FACILITATOR_BEARER_TOKEN": "test-token",
        "MPP_CHALLENGE_SECRET": "test-challenge-secret",
        **overrides,
    }
    return Settings(**settings_data)


def test_single_token_mode_requires_facilitator_bearer_token() -> None:
    with pytest.raises(ValueError, match="FACILITATOR_BEARER_TOKEN is required"):
        build_settings(FACILITATOR_BEARER_TOKEN=None)


@pytest.mark.parametrize(
    ("gateway_auth_mode", "facilitator_bearer_token"),
    [
        ("single_token", "test-token"),
        ("redis_gateways", None),
    ],
)
def test_runtime_requires_redis_url(
    gateway_auth_mode: str,
    facilitator_bearer_token: str | None,
) -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        build_settings(
            GATEWAY_AUTH_MODE=gateway_auth_mode,
            FACILITATOR_BEARER_TOKEN=facilitator_bearer_token,
            REDIS_URL=None,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_message"),
    [
        ("VALIDATION_TIMEOUT", 0, "greater than zero"),
        ("MAX_REQUEST_BODY_BYTES", 0, "greater than zero"),
        ("MIN_XRP_DROPS", -1, "zero or greater"),
        ("PAYCHANNEL_MIN_SETTLE_DELAY", -1, "zero or greater"),
        ("PAYCHANNEL_MAX_REDEMPTION_FEE_DROPS", 0, "greater than zero"),
    ],
)
def test_invalid_numeric_settings_fail_fast(
    field_name: str,
    field_value: int,
    error_message: str,
) -> None:
    with pytest.raises(ValueError, match=error_message):
        build_settings(**{field_name: field_value})


def test_zero_min_xrp_drops_is_allowed() -> None:
    settings = build_settings(MIN_XRP_DROPS=0)

    assert settings.MIN_XRP_DROPS == 0


def test_paychannel_idle_close_requires_background_redemption_interval() -> None:
    with pytest.raises(ValueError, match="IDLE_CLOSE_SECONDS requires"):
        build_settings(
            PAYCHANNEL_REDEEM_INTERVAL_SECONDS=0,
            PAYCHANNEL_IDLE_CLOSE_SECONDS=60,
        )


def test_paychannel_redemption_batch_is_bounded() -> None:
    with pytest.raises(ValueError, match="must not exceed 1000"):
        build_settings(PAYCHANNEL_REDEEM_BATCH_SIZE=1001)


def test_allowed_issued_assets_default_remains_empty() -> None:
    settings = build_settings()

    assert settings.ALLOWED_ISSUED_ASSETS == ""


def test_challenge_secret_rotation_keeps_active_key_first() -> None:
    settings = build_settings(
        MPP_CHALLENGE_SECRET="active",
        MPP_CHALLENGE_PREVIOUS_SECRETS="old-1, old-2",
    )

    assert settings.challenge_secrets() == ("active", "old-1", "old-2")


def test_network_requires_named_xrpl_network() -> None:
    with pytest.raises(ValueError, match="mainnet.*testnet.*devnet"):
        build_settings(NETWORK_ID="xrpl:1")


def test_xrpl_rpc_requires_tls_with_explicit_loopback_only_exception() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        build_settings(XRPL_RPC_URL="http://rippled.example:51234")
    with pytest.raises(ValueError, match="must use HTTPS"):
        build_settings(
            XRPL_RPC_URL="http://rippled.example:51234",
            ALLOW_INSECURE_XRPL_RPC=True,
        )

    configured = build_settings(
        XRPL_RPC_URL="http://127.0.0.1:5005",
        ALLOW_INSECURE_XRPL_RPC=True,
    )
    assert configured.XRPL_RPC_URL == "http://127.0.0.1:5005"


def test_compose_forwards_every_facilitator_setting() -> None:
    facilitator = compose_service_block("facilitator")

    for field_name in Settings.model_fields:
        assert f"{field_name}:" in facilitator, (
            f"docker compose does not forward facilitator setting {field_name}"
        )


def test_compose_forwards_buyer_policy_and_signer_settings() -> None:
    buyer = compose_service_block("buyer")
    payer_agent = compose_service_block("buyer-agent-mcp")
    shared_buyer_settings = {
        "XRPL_RPC_URL",
        "XRPL_NETWORK",
        "XRPL_WALLET_SEED",
        "XRPL_MPP_MAX_SPEND",
        "XRPL_MPP_EXPECTED_RECIPIENT",
    }
    for field_name in shared_buyer_settings:
        assert f"{field_name}:" in buyer
        assert f"{field_name}:" in payer_agent
    for field_name in {
        "ALLOW_INSECURE_XRPL_RPC",
        "XRPL_MPP_MAX_FEE_DROPS",
        "XRPL_MPP_IOU_SOURCE_CURRENCY",
        "XRPL_MPP_IOU_MAX_SOURCE_AMOUNT",
        "XRPL_MPP_IOU_SLIPPAGE_BPS",
    }:
        assert f"{field_name}:" in payer_agent
    assert "XRPL_MPP_RECEIPTS_PATH:" in payer_agent
    assert "PRICE_AMOUNT:" in buyer
    assert "MY_DESTINATION_ADDRESS:" in buyer
