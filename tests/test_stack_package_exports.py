from importlib import resources

import xrpl_mpp_client
from xrpl_mpp_client import XRPLPaymentSigner, build_payment_authorization, wrap_httpx_with_mpp_payment
import xrpl_mpp_core
from xrpl_mpp_core import PaymentChallenge, PaymentCredential, PaymentReceipt, XRPLAmount, XRPLAsset
import xrpl_mpp_facilitator
from xrpl_mpp_facilitator import create_app
import xrpl_mpp_middleware
from xrpl_mpp_middleware import PaymentMiddlewareASGI, XRPLFacilitatorClient, require_payment, require_session
import xrpl_mpp_payer
from xrpl_mpp_payer import XRPLPayer, pay_with_mpp


def test_stack_packages_export_expected_public_entrypoints() -> None:
    assert PaymentChallenge is not None
    assert PaymentCredential is not None
    assert PaymentReceipt is not None
    assert XRPLAmount is not None
    assert XRPLAsset is not None
    assert create_app is not None
    assert PaymentMiddlewareASGI is not None
    assert XRPLFacilitatorClient is not None
    assert require_payment is not None
    assert require_session is not None
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
    ):
        assert resources.files(package).joinpath("py.typed").is_file()
