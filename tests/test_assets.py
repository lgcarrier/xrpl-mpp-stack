import pytest

from xrpl_mpp_core import (
    LEGACY_RLUSD_TESTNET_ISSUER,
    RLUSD_MAINNET_ISSUER,
    RLUSD_HEX,
    RLUSD_TESTNET_ISSUER,
    USDC_HEX,
    USDC_MAINNET_ISSUER,
    USDC_TESTNET_ISSUER,
    parse_allowed_issued_assets,
    supported_asset_keys,
    xrpl_currency_code,
)


def test_rlusd_testnet_issuer_constants_identify_current_and_former_issuers() -> None:
    assert RLUSD_TESTNET_ISSUER == "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
    assert LEGACY_RLUSD_TESTNET_ISSUER == "rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2"


def test_supported_asset_keys_include_builtin_mainnet_issued_assets() -> None:
    assets = supported_asset_keys("mainnet", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_MAINNET_ISSUER),
        ("USDC", USDC_MAINNET_ISSUER),
    ]


def test_supported_asset_keys_include_builtin_testnet_issued_assets() -> None:
    assets = supported_asset_keys("testnet", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_TESTNET_ISSUER),
        ("USDC", USDC_TESTNET_ISSUER),
    ]


def test_supported_asset_keys_deduplicate_builtin_and_extra_assets() -> None:
    assets = supported_asset_keys(
        "testnet",
        f"USDC:{USDC_TESTNET_ISSUER},EUR:rExtraIssuer,RLUSD:{RLUSD_TESTNET_ISSUER}",
    )

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_TESTNET_ISSUER),
        ("USDC", USDC_TESTNET_ISSUER),
        ("EUR", "rExtraIssuer"),
    ]


def test_former_testnet_rlusd_issuer_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="former XRPL Testnet RLUSD issuer"):
        parse_allowed_issued_assets(f"RLUSD:{LEGACY_RLUSD_TESTNET_ISSUER}")


def test_xrpl_currency_code_keeps_standard_codes_and_hex_encodes_longer_codes() -> None:
    assert xrpl_currency_code("USD") == "USD"
    assert xrpl_currency_code("RLUSD") == RLUSD_HEX
    assert xrpl_currency_code("USDC") == USDC_HEX
    assert xrpl_currency_code(RLUSD_HEX.lower()) == RLUSD_HEX
