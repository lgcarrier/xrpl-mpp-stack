from importlib import resources
import inspect

import xrpl_mpp_client
from xrpl_mpp_client import (
    XRPLPaymentSigner,
    build_payment_authorization,
    wrap_httpx_with_mpp_payment,
)
import xrpl_mpp_core
from xrpl_mpp_core import (
    PaymentChallenge,
    PaymentCredential,
    PaymentReceipt,
    XRPLChargeRequest,
    XRPLSessionRequest,
)
import xrpl_mpp_facilitator
from xrpl_mpp_facilitator import create_app
import xrpl_mpp_middleware
from xrpl_mpp_middleware import (
    HookDispatcher,
    PaymentMiddlewareASGI,
    PaymentOutcomeRelay,
    XRPLFacilitatorClient,
    require_payment,
    require_session,
)
import xrpl_mpp_mcp
from xrpl_mpp_mcp import CallbackPaymentProcessor
import xrpl_mpp_payer
from xrpl_mpp_payer import XRPLPayer, pay_with_mpp
from xrpl_mpp_facilitator.xrpl_service import XRPLService
from xrpl_mpp_payer import payer as payer_module
from xrpl_mpp_payer.receipts import ReceiptRecord


def test_stack_packages_export_expected_public_entrypoints() -> None:
    assert PaymentChallenge is not None
    assert PaymentCredential is not None
    assert PaymentReceipt is not None
    assert XRPLChargeRequest is not None
    assert XRPLSessionRequest is not None
    assert create_app is not None
    assert PaymentMiddlewareASGI is not None
    assert XRPLFacilitatorClient is not None
    assert require_payment is not None
    assert require_session is not None
    assert HookDispatcher is not None
    assert PaymentOutcomeRelay is not None
    assert CallbackPaymentProcessor is not None
    assert XRPLPaymentSigner is not None
    assert build_payment_authorization is not None
    assert wrap_httpx_with_mpp_payment is not None
    assert XRPLPayer is not None
    assert pay_with_mpp is not None


def test_stack_packages_ship_pep_561_markers() -> None:
    for package in (
        xrpl_mpp_core,
        xrpl_mpp_client,
        xrpl_mpp_facilitator,
        xrpl_mpp_middleware,
        xrpl_mpp_payer,
        xrpl_mpp_mcp,
    ):
        assert resources.files(package).joinpath("py.typed").is_file()


def test_clean_break_omits_pre_02_public_compatibility_apis() -> None:
    stale_core_names = {
        "StructuredAmount",
        "XRPLAmount",
        "XRPLAsset",
        "amount_from_structured_amount",
        "asset_identifier_from_parts",
        "build_xrpl_extra",
        "canonical_asset_identifier",
        "parse_asset_identifier",
        "payment_option_matches",
        "xrpl_asset_from_identifier",
    }
    assert stale_core_names.isdisjoint(vars(xrpl_mpp_core))
    assert "asset" not in inspect.signature(
        xrpl_mpp_client.select_payment_challenge
    ).parameters
    assert "asset" not in inspect.signature(XRPLPayer).parameters
    assert not hasattr(payer_module, "resolve_asset_identifier")
    assert "asset_identifier" not in vars(ReceiptRecord)
    assert "tx_hash" not in vars(ReceiptRecord)
    assert not hasattr(XRPLService, "verify_payment")
    assert not hasattr(XRPLService, "settle_payment")
