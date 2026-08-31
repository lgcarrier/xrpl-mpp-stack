from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from xrpl.core import binarycodec
from xrpl.core.keypairs import is_valid_message
from xrpl.models.response import Response
from xrpl.models.requests import AccountInfo, RipplePathFind
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    AUTHORIZATION_HEADER,
    MPP_SOURCE_TAG,
    PAYMENT_RECEIPT_HEADER,
    PaymentPolicyError,
    PaymentRequestBindingError,
    XRPLIOUPathfindingPolicy,
    XRPLPathfindingError,
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    XRPL_RPC_URLS,
    build_payment_authorization,
    decode_payment_challenges_response,
    derive_paychannel_open_binding,
    last_ledger_sequence_from_expires,
    select_payment_challenge,
    wrap_httpx_with_mpp_payment,
)
from xrpl_mpp_core import (
    ACCEPT_PAYMENT_HEADER,
    PAYMENT_AUTHORIZATION_HEADER,
    AcceptPaymentRange,
    IssuedCurrency,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLMemo,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    build_content_digest,
    challenge_invoice_id,
    decode_charge_payload,
    decode_payment_credential,
    decode_session_payload,
    encode_payment_receipt,
    render_payment_challenge,
    serialize_currency,
)

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
REALM = "merchant.example"
SECRET = "client-test-secret-minimum-32-bytes"
ISSUER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
SOURCE_ISSUER = "r3kmLJN5D28dHuH8vZNUZpMC43pEHpaocV"
ISSUED_CURRENCY = serialize_currency(IssuedCurrency(currency="USD", issuer=ISSUER))
SOURCE_CURRENCY = serialize_currency(
    IssuedCurrency(currency="EUR", issuer=SOURCE_ISSUER)
)


def _payment_policy(
    *,
    expected_recipients: str = DESTINATION,
    max_amount: str = "1000",
    allowed_currencies: tuple[str, ...] = ("XRP",),
) -> XRPLPaymentPolicy:
    return XRPLPaymentPolicy(
        expected_recipients=expected_recipients,
        max_amount=max_amount,
        allowed_currencies=allowed_currencies,
    )


def _charge_challenge(
    *,
    network: str = "testnet",
    amount: str = "1000",
    currency: str = "XRP",
    header: str | None = None,
    invoice_id: str | None = "A" * 64,
    digest: str | None = None,
):
    return build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount=amount,
            currency=currency,
            recipient=DESTINATION,
            description="premium access",
            methodDetails=XRPLChargeMethodDetails(
                network=network,
                invoiceId=invoice_id,
                destinationTag=7,
                memos=[XRPLMemo(type="mpp", data="order-42")],
            ),
        ),
        expires_in_seconds=300,
        header=header,
        digest=digest,
    )


def _session_challenge(
    *,
    channel_id: str,
    amount: str = "25",
    cumulative: str | None = "100",
):
    return build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount=amount,
            currency="XRP",
            channelId=channel_id,
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                cumulativeAmount=cumulative,
            ),
        ),
        expires_in_seconds=300,
    )


def test_challenge_selection_uses_named_network_and_currency() -> None:
    testnet = _charge_challenge(network="testnet")
    mainnet = _charge_challenge(network="mainnet")
    headers = httpx.Headers(
        [
            ("WWW-Authenticate", render_payment_challenge(mainnet)),
            ("WWW-Authenticate", render_payment_challenge(testnet)),
        ]
    )

    decoded = decode_payment_challenges_response(headers)

    assert select_payment_challenge(decoded, network="testnet", currency="XRP") == testnet


@pytest.mark.parametrize("network", ["mainnet", "testnet", "devnet"])
def test_signer_default_rpc_url_follows_named_network(network: str) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network=network, autofill_enabled=False)

    assert signer.rpc_url == XRPL_RPC_URLS[network]
    assert signer._client.url == XRPL_RPC_URLS[network]


def test_signer_custom_rpc_url_overrides_named_network_default() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        rpc_url="https://rpc.example",
        autofill_enabled=False,
    )

    assert signer.rpc_url == "https://rpc.example"
    assert signer._client.url == "https://rpc.example"


@pytest.mark.parametrize(
    "rpc_url",
    ["http://rpc.example", "http://127.0.0.1:5005"],
)
def test_signer_rejects_plaintext_rpc_by_default(rpc_url: str) -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        XRPLPaymentSigner(
            Wallet.create(),
            network="testnet",
            rpc_url=rpc_url,
            autofill_enabled=False,
        )


def test_signer_allows_plaintext_rpc_only_for_explicit_loopback_development() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        rpc_url="http://127.0.0.1:5005",
        allow_insecure_rpc=True,
        autofill_enabled=False,
    )

    assert signer.rpc_url == "http://127.0.0.1:5005"

    with pytest.raises(ValueError, match="must use HTTPS"):
        XRPLPaymentSigner(
            Wallet.create(),
            network="testnet",
            rpc_url="http://rpc.example",
            allow_insecure_rpc=True,
            autofill_enabled=False,
        )


def test_signer_validates_custom_client_rpc_url() -> None:
    class PlaintextRemoteClient:
        url = "http://rpc.example"

    with pytest.raises(ValueError, match="must use HTTPS"):
        XRPLPaymentSigner(
            Wallet.create(),
            network="testnet",
            client=PlaintextRemoteClient(),
            allow_insecure_rpc=True,
            autofill_enabled=False,
        )


def test_charge_signer_builds_exact_bound_payment_and_source_did() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        expected_recipient=DESTINATION,
        max_amount="1000",
        allowed_currencies=["XRP"],
    )

    credential = signer.build_charge_credential(_charge_challenge())
    payload = decode_charge_payload(credential)
    transaction = binarycodec.decode(payload.blob)

    assert payload.type == "transaction"
    assert credential.source == f"did:pkh:xrpl:testnet:{signer.wallet.classic_address}"
    assert transaction["Account"] == signer.wallet.classic_address
    assert transaction["Destination"] == DESTINATION
    assert transaction["Amount"] == "1000"
    assert transaction["InvoiceID"] == "A" * 64
    assert transaction["DestinationTag"] == 7
    assert transaction["SourceTag"] == MPP_SOURCE_TAG
    assert bytes.fromhex(transaction["Memos"][0]["Memo"]["MemoData"]).decode() == "order-42"


def test_charge_signer_derives_invoice_id_and_supports_push_hash() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge(invoice_id=None)

    pull = signer.build_charge_credential(challenge)
    transaction = binarycodec.decode(decode_charge_payload(pull).blob)
    push = signer.build_hash_credential(challenge, transaction_hash="B" * 64)

    assert transaction["InvoiceID"] == challenge_invoice_id(challenge.id)
    assert decode_charge_payload(push).hash == "B" * 64
    assert push.source == pull.source


@pytest.mark.parametrize(
    ("autofilled_last_ledger", "expected_last_ledger"),
    [(2_000, None), (1_010, 1_010)],
)
def test_autofilled_charge_tightens_but_never_extends_last_ledger_sequence(
    monkeypatch,
    autofilled_last_ledger: int,
    expected_last_ledger: int | None,
) -> None:
    current_ledger = 1_000

    class FakeClient:
        def request(self, request):
            return Response(
                status="success",
                result={"ledger_current_index": current_ledger},
            )

    def fake_autofill(transaction: Payment, _client) -> Payment:
        return Payment.from_dict(
            {
                **transaction.to_dict(),
                "fee": "12",
                "sequence": 1,
                "last_ledger_sequence": autofilled_last_ledger,
            }
        )

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", fake_autofill)
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=FakeClient(),
    )

    credential = signer.build_charge_credential(_charge_challenge())
    transaction = binarycodec.decode(decode_charge_payload(credential).blob)

    if expected_last_ledger is None:
        assert current_ledger < transaction["LastLedgerSequence"] <= current_ledger + 75
    else:
        assert transaction["LastLedgerSequence"] == expected_last_ledger


def test_last_ledger_sequence_expiry_cap_keeps_one_ledger_of_room() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="less than one XRPL ledger interval"):
        last_ledger_sequence_from_expires(
            current_ledger_sequence=1_000,
            expires=(now + timedelta(seconds=4)).isoformat(),
            now=now,
        )


def test_autofilled_channel_create_accepts_challenge_expiry_cap(monkeypatch) -> None:
    current_ledger = 5_000

    class FakeClient:
        def request(self, request):
            return Response(
                status="success",
                result={"ledger_current_index": current_ledger},
            )

    def fake_autofill(transaction, _client):
        return type(transaction).from_dict(
            {
                **transaction.to_dict(),
                "fee": "12",
                "sequence": 1,
                "last_ledger_sequence": 9_000,
            }
        )

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", fake_autofill)
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=FakeClient(),
    )
    expires = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()

    blob = signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="1000000",
        settle_delay=3600,
        challenge_expires=expires,
    )
    transaction = binarycodec.decode(blob)

    assert current_ledger < transaction["LastLedgerSequence"] <= current_ledger + 15


def test_async_channel_create_does_not_nest_xrpl_autofill_event_loop(monkeypatch) -> None:
    async def async_autofill(transaction):
        await asyncio.sleep(0)
        return type(transaction).from_dict(
            {
                **transaction.to_dict(),
                "fee": "12",
                "sequence": 1,
                "last_ledger_sequence": 1_010,
            }
        )

    def xrpl_sync_autofill(transaction, _client):
        # xrpl.transaction.autofill is a synchronous asyncio.run() wrapper.
        return asyncio.run(async_autofill(transaction))

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", xrpl_sync_autofill)
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet")

    blob = asyncio.run(
        signer.sign_channel_create_async(
            destination=DESTINATION,
            funding_amount="1000000",
            settle_delay=3600,
        )
    )

    transaction = binarycodec.decode(blob)
    assert transaction["TransactionType"] == "PaymentChannelCreate"
    assert transaction["LastLedgerSequence"] == 1_010


def test_autofill_preserves_explicit_transaction_fields(monkeypatch) -> None:
    def fake_autofill(transaction: Payment, _client) -> Payment:
        return Payment.from_dict(
            {
                **transaction.to_dict(),
                "fee": "999",
                "sequence": 999,
                "last_ledger_sequence": 9_999,
                "destination": ISSUER,
                "amount": "999999",
                "flags": 0x00020000,
            }
        )

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", fake_autofill)
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet")

    blob = signer.sign_payment(
        pay_to=DESTINATION,
        currency="XRP",
        amount="1000",
        invoice_id="A" * 64,
        fee="20",
        sequence=7,
        last_ledger_sequence=1_010,
    )
    transaction = binarycodec.decode(blob)

    assert transaction["Fee"] == "20"
    assert transaction["Sequence"] == 7
    assert transaction["LastLedgerSequence"] == 1_010
    assert transaction["Destination"] == DESTINATION
    assert transaction["Amount"] == "1000"
    assert int(transaction.get("Flags", 0)) & 0x00020000 == 0


def test_signer_rejects_autofilled_fee_above_configured_maximum(monkeypatch) -> None:
    def fake_autofill(transaction: Payment, _client) -> Payment:
        return Payment.from_dict(
            {
                **transaction.to_dict(),
                "fee": "1001",
                "sequence": 1,
                "last_ledger_sequence": 1_010,
            }
        )

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", fake_autofill)
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        max_fee_drops="1000",
    )

    with pytest.raises(ValueError, match="configured maximum is 1000 drops"):
        signer.sign_payment(
            pay_to=DESTINATION,
            currency="XRP",
            amount="1000",
            invoice_id="A" * 64,
        )


def test_signer_rejects_explicit_fee_above_maximum_before_autofill(monkeypatch) -> None:
    def unexpected_autofill(*_args, **_kwargs):
        pytest.fail("autofill must not run for a fee rejected by local policy")

    monkeypatch.setattr("xrpl_mpp_client.signer.autofill", unexpected_autofill)
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        max_fee_drops="1000",
    )

    with pytest.raises(ValueError, match="configured maximum is 1000 drops"):
        signer.sign_payment(
            pay_to=DESTINATION,
            currency="XRP",
            amount="1000",
            invoice_id="A" * 64,
            fee="1001",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_fee_drops": "0"}, "max_fee_drops"),
        ({"max_fee_drops": "11", "default_fee": "12"}, "default_fee cannot exceed"),
    ],
)
def test_signer_validates_fee_policy_configuration(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        XRPLPaymentSigner(
            Wallet.create(),
            network="testnet",
            autofill_enabled=False,
            **kwargs,
        )


def test_issued_payment_defaults_to_direct_exact_send_max() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )

    blob = signer.sign_payment(
        pay_to=DESTINATION,
        currency=ISSUED_CURRENCY,
        amount="100",
        invoice_id="A" * 64,
    )
    transaction = binarycodec.decode(blob)

    assert transaction["Amount"] == {
        "currency": "USD",
        "issuer": ISSUER,
        "value": "100",
    }
    assert transaction["SendMax"] == transaction["Amount"]
    assert "Paths" not in transaction
    assert int(transaction.get("Flags", 0)) & 0x00020000 == 0


def test_destination_payment_policy_rejects_before_iou_path_rpc() -> None:
    class MustNotCallClient:
        def request(self, _request):
            pytest.fail("path RPC must not run before destination policy authorization")

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=MustNotCallClient(),
        autofill_enabled=False,
        payment_policy=XRPLPaymentPolicy(
            expected_recipients=DESTINATION,
            max_amount="10",
            allowed_currencies=[ISSUED_CURRENCY],
        ),
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency="XRP",
            max_source_amount="1000",
            retry_delays_seconds=(),
        ),
    )

    with pytest.raises(PaymentPolicyError, match="max_amount"):
        signer.build_charge_credential(
            _charge_challenge(amount="100", currency=ISSUED_CURRENCY)
        )


def test_self_issued_payment_omits_send_max() -> None:
    wallet = Wallet.create()
    currency = serialize_currency(
        IssuedCurrency(currency="USD", issuer=wallet.classic_address)
    )
    signer = XRPLPaymentSigner(wallet, network="testnet", autofill_enabled=False)

    blob = signer.sign_payment(
        pay_to=DESTINATION,
        currency=currency,
        amount="100",
        invoice_id="A" * 64,
    )

    assert "SendMax" not in binarycodec.decode(blob)


def test_iou_direct_route_caps_transfer_rate_and_slippage() -> None:
    class DirectClient:
        def request(self, request):
            assert isinstance(request, AccountInfo)
            assert request.account == ISSUER
            return Response(
                status="success",
                result={"account_data": {"TransferRate": 1_005_000_000}},
            )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=DirectClient(),
        autofill_enabled=False,
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency=ISSUED_CURRENCY,
            max_source_amount="102",
            slippage_bps=50,
            retry_delays_seconds=(),
        ),
    )

    blob = signer.sign_payment(
        pay_to=DESTINATION,
        currency=ISSUED_CURRENCY,
        amount="100",
        invoice_id="A" * 64,
    )
    transaction = binarycodec.decode(blob)

    assert transaction["Amount"]["value"] == "100"
    assert transaction["SendMax"] == {
        "currency": "USD",
        "issuer": ISSUER,
        "value": "101.0025",
    }
    assert "Paths" not in transaction


def test_cross_currency_path_is_source_allowlisted_and_absolutely_capped() -> None:
    class PathClient:
        def request(self, request):
            assert isinstance(request, RipplePathFind)
            assert len(request.source_currencies or []) == 1
            assert type(request.source_currencies[0]).__name__ == "XRP"
            return Response(
                status="success",
                result={
                    "alternatives": [
                        {
                            "source_amount": "1000",
                            "paths_computed": [
                                [{"currency": "USD", "issuer": ISSUER}]
                            ],
                        }
                    ]
                },
            )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=PathClient(),
        autofill_enabled=False,
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency="XRP",
            max_source_amount="1005",
            slippage_bps=50,
            retry_delays_seconds=(),
        ),
    )

    blob = signer.sign_payment(
        pay_to=DESTINATION,
        currency=ISSUED_CURRENCY,
        amount="100",
        invoice_id="A" * 64,
    )
    transaction = binarycodec.decode(blob)

    assert transaction["Amount"]["value"] == "100"
    assert transaction["SendMax"] == "1005"
    assert transaction["Paths"] == [[{"currency": "USD", "issuer": ISSUER}]]
    assert int(transaction.get("Flags", 0)) & 0x00020000 == 0


def test_cross_currency_route_rejects_source_spend_above_local_cap() -> None:
    class ExpensivePathClient:
        def request(self, _request):
            return Response(
                status="success",
                result={
                    "alternatives": [
                        {"source_amount": "1000", "paths_computed": []}
                    ]
                },
            )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=ExpensivePathClient(),
        autofill_enabled=False,
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency="XRP",
            max_source_amount="1004",
            slippage_bps=50,
            retry_delays_seconds=(),
        ),
    )

    with pytest.raises(XRPLPathfindingError, match="maximum is 1004"):
        signer.sign_payment(
            pay_to=DESTINATION,
            currency=ISSUED_CURRENCY,
            amount="100",
            invoice_id="A" * 64,
        )


def test_path_quote_cannot_change_authorized_source_currency() -> None:
    class WrongSourceClient:
        def request(self, _request):
            return Response(
                status="success",
                result={
                    "alternatives": [
                        {
                            "source_amount": {
                                "currency": "USD",
                                "issuer": ISSUER,
                                "value": "1",
                            },
                            "paths_computed": [],
                        }
                    ]
                },
            )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=WrongSourceClient(),
        autofill_enabled=False,
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency=SOURCE_CURRENCY,
            max_source_amount="2",
            retry_delays_seconds=(),
        ),
    )

    with pytest.raises(XRPLPathfindingError, match="changed the authorized source"):
        signer.sign_payment(
            pay_to=DESTINATION,
            currency=ISSUED_CURRENCY,
            amount="1",
            invoice_id="A" * 64,
        )


def test_path_quote_must_fit_xrpl_pathset_bounds() -> None:
    class OversizedPathClient:
        def request(self, _request):
            return Response(
                status="success",
                result={
                    "alternatives": [
                        {
                            "source_amount": "1",
                            "paths_computed": [
                                [{"currency": "USD", "issuer": ISSUER}]
                                for _ in range(7)
                            ],
                        }
                    ]
                },
            )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        client=OversizedPathClient(),
        autofill_enabled=False,
        iou_pathfinding_policy=XRPLIOUPathfindingPolicy(
            source_currency="XRP",
            max_source_amount="1",
            slippage_bps=0,
            retry_delays_seconds=(),
        ),
    )

    with pytest.raises(XRPLPathfindingError, match="path-count limit"):
        signer.sign_payment(
            pay_to=DESTINATION,
            currency=ISSUED_CURRENCY,
            amount="1",
            invoice_id="A" * 64,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_currency": "XRP", "max_source_amount": "1.5"}, "drops"),
        (
            {
                "source_currency": "XRP",
                "max_source_amount": "1",
                "slippage_bps": 1001,
            },
            "between 0 and 1000",
        ),
        (
            {
                "source_currency": "XRP",
                "max_source_amount": "1",
                "retry_delays_seconds": (3.0, 3.0, 3.0),
            },
            "cannot exceed seven seconds",
        ),
        (
            {
                "source_currency": "XRP",
                "max_source_amount": "1",
                "retry_delays_seconds": (float("nan"),),
            },
            "retry delays",
        ),
        (
            {
                "source_currency": '{"mpt_issuance_id":"' + "A" * 64 + '"}',
                "max_source_amount": "1",
            },
            "MPT assets do not support",
        ),
    ],
)
def test_iou_pathfinding_policy_is_bounded(kwargs, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        XRPLIOUPathfindingPolicy(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expected_recipient": Wallet.create().classic_address}, "recipient"),
        ({"max_amount": "999"}, "max_amount"),
        ({"allowed_currencies": ['{"currency":"USD","issuer":"rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"}']}, "currency"),
    ],
)
def test_charge_signer_policy_guardrails_fail_before_signing(kwargs, message: str) -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        **kwargs,
    )

    with pytest.raises(ValueError, match=message):
        signer.build_charge_credential(_charge_challenge())


def test_signer_rejects_non_xrpl_method_before_signing() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )
    challenge = _charge_challenge().model_copy(update={"method": "tempo"})

    with pytest.raises(ValueError, match="method must be xrpl"):
        signer.build_charge_credential(challenge)


def test_paychannel_voucher_is_cumulative_and_cryptographically_valid() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _session_challenge(channel_id="C" * 64)

    credential = signer.build_session_voucher_credential(challenge)
    payload = decode_session_payload(credential)
    message = bytes.fromhex(
        binarycodec.encode_for_signing_claim(
            {"channel": payload.channel_id, "amount": payload.amount}
        )
    )

    assert payload.action == "voucher"
    assert payload.amount == "125"
    assert is_valid_message(
        message,
        bytes.fromhex(payload.signature),
        signer.wallet.public_key,
    )


def test_paychannel_open_signs_real_channel_id_for_initial_cumulative_claim() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    open_transaction = signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="1000000",
        settle_delay=3600,
        cancel_after=900_000_000,
    )
    decoded = binarycodec.decode(open_transaction)
    challenge = _session_challenge(channel_id="", amount="25", cumulative="0")

    credential = signer.build_session_open_credential(
        challenge,
        open_transaction=open_transaction,
    )
    payload = decode_session_payload(credential)
    binding = derive_paychannel_open_binding(open_transaction)
    message = bytes.fromhex(
        binarycodec.encode_for_signing_claim(
            {"channel": binding.channel_id, "amount": payload.amount}
        )
    )

    assert decoded["TransactionType"] == "PaymentChannelCreate"
    assert decoded["Destination"] == DESTINATION
    assert decoded["Amount"] == "1000000"
    assert decoded["PublicKey"] == signer.wallet.public_key
    assert payload.action == "open"
    assert payload.amount == "25"
    assert is_valid_message(
        message,
        bytes.fromhex(payload.signature),
        signer.wallet.public_key,
    )


def test_paychannel_open_rejects_mismatched_transaction_parties() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _session_challenge(channel_id="", amount="1", cumulative="0")
    other_signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )
    wrong_payer = other_signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="1000",
        settle_delay=3600,
    )
    with pytest.raises(ValueError, match="payer does not match"):
        signer.build_session_open_credential(challenge, open_transaction=wrong_payer)

    wrong_recipient = signer.sign_channel_create(
        destination=Wallet.create().classic_address,
        funding_amount="1000",
        settle_delay=3600,
    )
    with pytest.raises(ValueError, match="recipient does not match"):
        signer.build_session_open_credential(challenge, open_transaction=wrong_recipient)


def test_paychannel_open_rejects_initial_claim_above_funding() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    open_transaction = signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="24",
        settle_delay=3600,
    )
    challenge = _session_challenge(channel_id="", amount="25", cumulative="0")

    with pytest.raises(ValueError, match="exceeds PaymentChannelCreate funding"):
        signer.build_session_open_credential(
            challenge,
            open_transaction=open_transaction,
        )


def test_httpx_transport_retries_charge_in_selected_header_and_preserves_bearer() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        expected_recipient=DESTINATION,
        max_amount="1000",
        allowed_currencies=["XRP"],
    )
    challenge = _charge_challenge(header=PAYMENT_AUTHORIZATION_HEADER)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers[AUTHORIZATION_HEADER] == "Bearer identity-token"
        assert request.headers[ACCEPT_PAYMENT_HEADER] == "xrpl/charge, xrpl/session"
        if attempts == 1:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        authorization = request.headers[PAYMENT_AUTHORIZATION_HEADER]
        assert authorization.startswith("Payment ")
        credential = decode_payment_credential(authorization.removeprefix("Payment "))
        payload = decode_charge_payload(credential)
        assert payload.type == "transaction"
        transaction = Payment.from_xrpl(binarycodec.decode(payload.blob))
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference=transaction.get_hash().upper(),
        )
        return httpx.Response(
            200,
            json={"ok": True},
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    async def run() -> httpx.Response:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(handler),
            base_url="https://merchant.example",
            currency="XRP",
        ) as client:
            return await client.get(
                "/paid",
                headers={AUTHORIZATION_HEADER: "Bearer identity-token"},
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert attempts == 2


def test_httpx_transport_does_not_forward_payment_credential_across_redirect() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        expected_recipient=DESTINATION,
        max_amount="1000",
        allowed_currencies=["XRP"],
    )
    challenge = _charge_challenge(header=PAYMENT_AUTHORIZATION_HEADER)
    merchant_attempts = 0
    evil_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal merchant_attempts, evil_requests
        if request.url.host == "evil.example":
            evil_requests += 1
            assert PAYMENT_AUTHORIZATION_HEADER not in request.headers
            authorization = request.headers.get(AUTHORIZATION_HEADER, "")
            assert not authorization.startswith("Payment ")
            return httpx.Response(200, json={"received": False}, request=request)

        merchant_attempts += 1
        if PAYMENT_AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        assert request.headers[PAYMENT_AUTHORIZATION_HEADER].startswith("Payment ")
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/capture"},
            request=request,
        )

    async def run() -> httpx.Response:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
            payment_policy=_payment_policy(),
        )
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
        ) as client:
            return await client.get(
                "https://merchant.example/paid",
                headers={AUTHORIZATION_HEADER: "Bearer identity-token"},
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.url == httpx.URL("https://evil.example/capture")
    assert merchant_attempts == 2
    assert evil_requests == 1


def test_httpx_transport_rejects_forged_charge_receipt_reference() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        expected_recipient=DESTINATION,
        max_amount="1000",
        allowed_currencies=["XRP"],
    )
    challenge = _charge_challenge()

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        forged = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference="F" * 64,
        )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(forged)},
            request=request,
        )

    async def run() -> None:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(handler),
            base_url="https://merchant.example",
            currency="XRP",
        ) as client:
            await client.get("/paid")

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(run())


def test_httpx_transport_accepts_core_only_push_charge_receipt() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge()
    transaction_hash = "B" * 64
    credential = signer.build_hash_credential(
        challenge,
        transaction_hash=transaction_hash,
    )
    receipt = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=transaction_hash,
    )
    response = httpx.Response(
        200,
        headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
        request=httpx.Request("GET", "https://merchant.example/paid"),
    )
    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(lambda request: response),
        payment_policy=_payment_policy(),
    )

    transport._validate_charge_success(challenge, credential, response)


def test_httpx_transport_resumes_registered_channel_and_advances_cumulative() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id)
    captured_amount = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_amount
        authorization = request.headers.get(AUTHORIZATION_HEADER)
        if authorization is None:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        credential = decode_payment_credential(authorization.removeprefix("Payment "))
        captured_amount = decode_session_payload(credential).amount
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference=f"{channel_id}:{captured_amount}",
        )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/metered",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get("https://merchant.example/metered")

    response = asyncio.run(run())
    state = transport._sessions["GET https://merchant.example/metered"]

    assert response.status_code == 200
    assert captured_amount == "125"
    assert state.cumulative_amount == "125"


def test_httpx_voucher_does_not_advance_on_bare_success() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(200, request=request)

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/metered",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get("https://merchant.example/metered")

    assert asyncio.run(run()).status_code == 200
    assert transport.channel_state(
        "https://merchant.example/metered"
    ).cumulative_amount == "100"


def test_httpx_voucher_rejects_mismatched_receipt_without_advancing() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id)
    poisoned = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"{channel_id}:999",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(poisoned)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/metered",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/metered")

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(run())
    assert transport.channel_state(
        "https://merchant.example/metered"
    ).cumulative_amount == "100"


@pytest.mark.parametrize("server_cumulative", [None, "99", "1000000"])
def test_httpx_transport_rejects_untrusted_session_cumulative_before_signing(
    monkeypatch,
    server_cumulative: str | None,
) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(
        channel_id=channel_id,
        amount="1",
        cumulative=server_cumulative,
    )
    attempts = 0

    async def must_not_sign(*_args, **_kwargs) -> None:
        pytest.fail("automatic transport signed a cumulative-mismatched challenge")

    monkeypatch.setattr(signer, "build_session_voucher_credential_async", must_not_sign)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(max_amount="1"),
    )
    transport.register_channel(
        "https://merchant.example/metered",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/metered")

    with pytest.raises(ValueError, match="cumulativeAmount"):
        asyncio.run(run())
    assert attempts == 1


def test_httpx_transport_requires_local_state_before_signing_session_voucher(
    monkeypatch,
) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _session_challenge(channel_id="C" * 64)

    async def must_not_sign(*_args, **_kwargs) -> None:
        pytest.fail("automatic transport signed a voucher without local state")

    monkeypatch.setattr(signer, "build_session_voucher_credential_async", must_not_sign)

    async def run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    402,
                    headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                    request=request,
                )
            ),
            payment_policy=_payment_policy(),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/metered")

    with pytest.raises(ValueError, match="registered local channel state"):
        asyncio.run(run())


@pytest.mark.parametrize("server_cumulative", [None, "99", "1000000"])
def test_httpx_close_rejects_untrusted_session_cumulative_before_signing(
    monkeypatch,
    server_cumulative: str | None,
) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(
        channel_id=channel_id,
        amount="0",
        cumulative=server_cumulative,
    )
    attempts = 0

    async def must_not_sign(*_args, **_kwargs) -> None:
        pytest.fail("automatic transport signed a cumulative-mismatched close")

    monkeypatch.setattr(signer, "build_session_close_credential_async", must_not_sign)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/close",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    async def run() -> httpx.Response:
        return await transport.close_session("https://merchant.example/close")

    response = asyncio.run(run())

    assert response.status_code == 402
    assert attempts == 1


def test_httpx_close_derives_cumulative_from_registered_local_state() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id, amount="25", cumulative="100")
    captured_amount = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_amount
        authorization = request.headers.get(AUTHORIZATION_HEADER)
        if authorization is None:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        credential = decode_payment_credential(authorization.removeprefix("Payment "))
        payload = decode_session_payload(credential)
        assert payload.action == "close"
        captured_amount = payload.amount
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference=f"{channel_id}:{captured_amount}",
        )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/close",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    response = asyncio.run(transport.close_session("https://merchant.example/close"))

    assert response.status_code == 200
    assert captured_amount == "125"
    assert "GET https://merchant.example/close" not in transport._sessions


def test_httpx_close_does_not_forget_channel_on_bare_success() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id, amount="25", cumulative="100")

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(200, request=request)

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/close",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    response = asyncio.run(transport.close_session("https://merchant.example/close"))

    assert response.status_code == 200
    assert transport.channel_state("https://merchant.example/close") is not None


def test_httpx_close_rejects_poisoned_receipt_without_forgetting_channel() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_id = "C" * 64
    challenge = _session_challenge(channel_id=channel_id, amount="25", cumulative="100")
    poisoned = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"{channel_id}:999",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(poisoned)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_channel(
        "https://merchant.example/close",
        channel_id=channel_id,
        cumulative_amount="100",
        network="testnet",
    )

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(transport.close_session("https://merchant.example/close"))
    assert transport.channel_state("https://merchant.example/close") is not None


def test_httpx_open_flow_requires_registered_transaction_and_captures_channel() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _session_challenge(channel_id="", amount="0", cumulative="0")
    transaction = signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="1000000",
        settle_delay=3600,
    )
    binding = derive_paychannel_open_binding(transaction)
    receipt = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"open:{binding.channel_id}:{binding.tx_hash}",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    async def run(registered: bool) -> tuple[httpx.Response, XRPLPaymentTransport]:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
            payment_policy=_payment_policy(),
        )
        if registered:
            transport.register_open_transaction(
                "https://merchant.example/open",
                transaction=transaction,
            )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://merchant.example/open")
        return response, transport

    with pytest.raises(ValueError, match="register_open_transaction"):
        asyncio.run(run(False))

    response, transport = asyncio.run(run(True))

    assert response.status_code == 200
    assert (
        transport._sessions["GET https://merchant.example/open"].channel_id
        == binding.channel_id
    )


def test_httpx_open_rejects_mismatched_core_receipt_reference_without_state() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _session_challenge(channel_id="", amount="0", cumulative="0")
    transaction = signer.sign_channel_create(
        destination=DESTINATION,
        funding_amount="1000000",
        settle_delay=3600,
    )
    forged = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"open:{'D' * 64}:{'E' * 64}",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(forged)},
            request=request,
        )

    transport = XRPLPaymentTransport(
        signer,
        base_transport=httpx.MockTransport(handler),
        payment_policy=_payment_policy(),
    )
    transport.register_open_transaction(
        "https://merchant.example/open",
        transaction=transaction,
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/open")

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(run())
    assert "GET https://merchant.example/open" not in transport._sessions
    assert "GET https://merchant.example/open" in transport._open_transactions


def test_httpx_transport_rejects_remote_plaintext_mpp() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    async def run() -> None:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            ),
            base_url="http://merchant.example",
        ) as client:
            await client.get("/paid")

    with pytest.raises(ValueError, match="require HTTPS"):
        asyncio.run(run())


def test_httpx_transport_requires_explicit_plaintext_loopback_opt_in() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    async def run(*, allow_insecure_localhost: bool) -> httpx.Response:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            ),
            base_url="http://127.0.0.1",
            allow_insecure_localhost=allow_insecure_localhost,
        ) as client:
            return await client.get("/paid")

    with pytest.raises(ValueError, match="require HTTPS"):
        asyncio.run(run(allow_insecure_localhost=False))

    response = asyncio.run(run(allow_insecure_localhost=True))
    assert response.status_code == 200


def test_httpx_transport_does_not_pay_a_q_zero_challenge() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    async def run() -> httpx.Response:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
            payment_preferences=[
                AcceptPaymentRange(method="xrpl", intent="charge", q="0")
            ],
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get("https://merchant.example/paid")

    response = asyncio.run(run())

    assert response.status_code == 402
    assert attempts == 1


def test_httpx_transport_fails_closed_without_complete_payment_policy(monkeypatch) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge()
    attempts = 0

    async def must_not_sign(_challenge) -> None:
        pytest.fail("automatic transport reached the signer without authorization")

    monkeypatch.setattr(signer, "build_charge_credential_async", must_not_sign)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    async def run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/paid")

    with pytest.raises(PaymentPolicyError, match="disabled without a complete payment policy"):
        asyncio.run(run())
    assert attempts == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_recipient": DESTINATION},
        {"max_amount": "1000"},
        {"allowed_currencies": ["XRP"]},
        {"expected_recipient": DESTINATION, "max_amount": "1000"},
        {"expected_recipient": DESTINATION, "allowed_currencies": ["XRP"]},
        {"max_amount": "1000", "allowed_currencies": ["XRP"]},
    ],
)
def test_partial_signer_guardrails_never_enable_automatic_payment(kwargs) -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        **kwargs,
    )

    assert signer.automatic_payment_policy is None


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            _payment_policy(expected_recipients=Wallet.create().classic_address),
            "recipient",
        ),
        (_payment_policy(max_amount="999"), "max_amount"),
        (
            _payment_policy(
                allowed_currencies=(
                    '{"currency":"USD","issuer":"rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"}',
                )
            ),
            "currency",
        ),
    ],
)
def test_httpx_transport_policy_rejects_unapproved_terms_before_signing(
    monkeypatch,
    policy: XRPLPaymentPolicy,
    message: str,
) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge()

    async def must_not_sign(_challenge) -> None:
        pytest.fail("automatic transport signed a policy-rejected challenge")

    monkeypatch.setattr(signer, "build_charge_credential_async", must_not_sign)

    async def run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    402,
                    headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                    request=request,
                )
            ),
            payment_policy=policy,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://merchant.example/paid")

    with pytest.raises(PaymentPolicyError, match=message):
        asyncio.run(run())


def test_httpx_transport_verifies_digest_and_replays_buffered_stream_body() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    body = b'{"bound":true}'
    challenge = _charge_challenge(digest=build_content_digest(body))
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.content == body
        if attempts == 1:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(200, request=request)

    async def stream_body():
        yield body[:5]
        yield body[5:]

    async def run() -> httpx.Response:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
            payment_policy=_payment_policy(),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post(
                "https://merchant.example/paid",
                content=stream_body(),
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert attempts == 2


def test_httpx_transport_rejects_digest_mismatch_before_signing(monkeypatch) -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    challenge = _charge_challenge(digest=build_content_digest(b"expected"))
    attempts = 0

    async def must_not_sign(_challenge) -> None:
        pytest.fail("automatic transport signed a body-mismatched challenge")

    monkeypatch.setattr(signer, "build_charge_credential_async", must_not_sign)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    async def run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            base_transport=httpx.MockTransport(handler),
            payment_policy=_payment_policy(),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post("https://merchant.example/paid", content=b"different")

    with pytest.raises(PaymentRequestBindingError, match="does not match"):
        asyncio.run(run())
    assert attempts == 1


@pytest.mark.parametrize("max_amount", ["-1", "NaN", "Infinity"])
def test_payment_policy_rejects_unsafe_amount_ceilings(max_amount: str) -> None:
    with pytest.raises(ValueError, match="non-negative finite"):
        _payment_policy(max_amount=max_amount)


@pytest.mark.parametrize(
    ("expires", "message"),
    [
        (None, "requires an expiring"),
        (
            (datetime.now(UTC) + timedelta(seconds=600)).isoformat().replace("+00:00", "Z"),
            "validity window",
        ),
    ],
)
def test_payment_policy_rejects_missing_or_excessive_validity_window(
    expires: str | None,
    message: str,
) -> None:
    challenge = _charge_challenge().model_copy(update={"expires": expires})

    with pytest.raises(PaymentPolicyError, match=message):
        _payment_policy().authorize(challenge)


def test_signer_with_explicit_policy_enforces_policy_validity_window() -> None:
    policy = _payment_policy()
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        payment_policy=policy,
    )
    challenge = _charge_challenge().model_copy(
        update={
            "expires": (datetime.now(UTC) + timedelta(seconds=600))
            .isoformat()
            .replace("+00:00", "Z")
        }
    )

    with pytest.raises(PaymentPolicyError, match="validity window"):
        signer.build_charge_credential(challenge)


def test_build_payment_authorization_round_trips_native_credential() -> None:
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    value = build_payment_authorization(signer.build_charge_credential(_charge_challenge()))

    assert value.startswith("Payment ")
    assert decode_charge_payload(
        decode_payment_credential(value.removeprefix("Payment "))
    ).type == "transaction"
