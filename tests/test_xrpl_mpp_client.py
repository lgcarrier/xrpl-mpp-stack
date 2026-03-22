from __future__ import annotations

import asyncio

import httpx
import pytest
from xrpl.core import binarycodec
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    AUTHORIZATION_HEADER,
    PAYMENT_RECEIPT_HEADER,
    WWW_AUTHENTICATE_HEADER,
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
    wrap_httpx_with_mpp_payment,
)
from xrpl_mpp_client.httpx import SessionState as ClientSessionState
from xrpl_mpp_core import (
    PaymentCredential,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    decode_challenge_request,
    decode_charge_payload,
    decode_payment_credential,
    encode_payment_receipt,
    render_payment_challenge,
)

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
REALM = "merchant.example"
SECRET = "client-test-secret"


def _charge_challenge(*, network: str = "xrpl:1", amount: str = "1000"):
    request = XRPLChargeRequest(
        amount=amount,
        currency="XRP:native",
        recipient=DESTINATION,
        description="premium access",
        methodDetails=XRPLChargeMethodDetails(
            network=network,
            invoiceId="A" * 64,
        ),
    )
    return build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="charge",
        request_model=request,
        expires_in_seconds=300,
    )


def test_decode_payment_challenges_response_accepts_multiple_www_authenticate_headers() -> None:
    charge = _charge_challenge(network="xrpl:1")
    secondary = _charge_challenge(network="xrpl:2")
    headers = httpx.Headers(
        [
            (WWW_AUTHENTICATE_HEADER, render_payment_challenge(charge)),
            (WWW_AUTHENTICATE_HEADER, render_payment_challenge(secondary)),
        ]
    )

    decoded = decode_payment_challenges_response(headers)

    assert [item.intent for item in decoded] == ["charge", "charge"]
    assert decoded[0].realm == REALM
    assert decoded[1].realm == REALM


def test_select_payment_challenge_filters_by_network() -> None:
    charge = _charge_challenge(network="xrpl:1")
    secondary = _charge_challenge(network="xrpl:2")

    selected = select_payment_challenge([secondary, charge], network="xrpl:1", asset="XRP:native")

    assert selected == charge


def test_build_payment_authorization_signs_offline_exact_xrp_payment() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    authorization = build_payment_authorization(signer.build_charge_credential(_charge_challenge()))
    credential = decode_payment_credential(authorization.removeprefix("Payment "))
    payload = decode_charge_payload(credential)
    tx = binarycodec.decode(payload.signed_tx_blob)

    assert tx["Destination"] == DESTINATION
    assert tx["Account"] == signer.wallet.classic_address
    assert tx["Amount"] == "1000"
    assert tx["InvoiceID"] == "A" * 64


def test_httpx_transport_retries_once_after_charge_402() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = _charge_challenge()
    receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123",
        challengeId=challenge.id,
        intent="charge",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash="ABC123",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
    )
    attempts = 0
    captured_authorization: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, captured_authorization
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                json={"status": 402, "title": "Payment Required", "detail": "Payment required"},
                request=request,
            )

        captured_authorization = request.headers.get(AUTHORIZATION_HEADER)
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            json={"ok": True},
            request=request,
        )

    async def _run() -> httpx.Response:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(handler),
            base_url="https://merchant.example",
            asset="XRP:native",
        ) as client:
            return await client.get("/paid")

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert attempts == 2
    assert captured_authorization is not None
    assert captured_authorization.startswith("Payment ")
    decoded_receipt = decode_payment_receipt_header(response.headers)
    assert decoded_receipt is not None
    assert decoded_receipt.tx_hash == "ABC123"


def test_httpx_transport_uses_async_signer_wrapper_for_charge() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
    )
    challenge = _charge_challenge()
    receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123",
        challengeId=challenge.id,
        intent="charge",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash="ABC123",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
    )

    def fail_if_called(_challenge):
        raise AssertionError("sync signer path should not be used inside async transport")

    async def fake_build_charge_credential_async(_challenge):
        return PaymentCredential(
            challenge=challenge,
            payload={"signedTxBlob": "DEADBEEF"},
        )

    signer.build_charge_credential = fail_if_called  # type: ignore[method-assign]
    signer.build_charge_credential_async = fake_build_charge_credential_async  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            json={"ok": True},
            request=request,
        )

    async def _run() -> httpx.Response:
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=httpx.MockTransport(handler),
            base_url="https://merchant.example",
            asset="XRP:native",
        ) as client:
            return await client.get("/paid")

    response = asyncio.run(_run())

    assert response.status_code == 200


def test_session_keys_distinguish_method_and_query() -> None:
    transport = XRPLPaymentTransport(
        XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)
    )

    get_basic = transport._session_key(
        httpx.Request("GET", "https://merchant.example/metered?plan=basic")
    )
    post_basic = transport._session_key(
        httpx.Request("POST", "https://merchant.example/metered?plan=basic")
    )
    get_premium = transport._session_key(
        httpx.Request("GET", "https://merchant.example/metered?plan=premium")
    )

    assert get_basic != post_basic
    assert get_basic != get_premium


def test_session_top_up_reuses_original_request_method_and_body() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )
    open_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="session-ref",
        challengeId=challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        sessionToken="session-token",
        settlementStatus="session_open",
        asset={"code": "XRP"},
        amount={"value": "250", "unit": "drops", "asset": {"code": "XRP"}, "drops": 250},
        availableBalance="750",
        prepaidTotal="1000",
        spentTotal="250",
        lastAction="open",
    )
    top_up_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:01:00Z",
        reference="session-ref",
        challengeId=challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        settlementStatus="session_active",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
        availableBalance="1750",
        prepaidTotal="2000",
        spentTotal="250",
        lastAction="top_up",
    )
    use_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:02:00Z",
        reference="session-ref",
        challengeId=challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        settlementStatus="session_active",
        asset={"code": "XRP"},
        amount={"value": "250", "unit": "drops", "asset": {"code": "XRP"}, "drops": 250},
        availableBalance="1500",
        prepaidTotal="2000",
        spentTotal="500",
        lastAction="use",
    )
    request_body = b'{"units":1}'
    attempts = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["value"] += 1
        authorization = request.headers.get(AUTHORIZATION_HEADER)
        if attempts["value"] == 1:
            assert request.method == "POST"
            assert request.content == request_body
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        if attempts["value"] == 2:
            assert request.method == "POST"
            assert request.content == request_body
            assert authorization is not None
            return httpx.Response(
                200,
                headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(open_receipt)},
                json={"ok": True},
                request=request,
            )
        if attempts["value"] == 3:
            assert request.method == "POST"
            assert request.content == request_body
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        if attempts["value"] == 4:
            assert request.method == "POST"
            assert request.content == request_body
            assert authorization is not None
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        if attempts["value"] == 5:
            assert request.method == "POST"
            assert request.content == request_body
            assert authorization is not None
            return httpx.Response(
                200,
                headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(top_up_receipt)},
                json={"top_up": True},
                request=request,
            )
        if attempts["value"] == 6:
            assert request.method == "POST"
            assert request.content == request_body
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        assert request.method == "POST"
        assert request.content == request_body
        assert authorization is not None
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(use_receipt)},
            json={"ok": True},
            request=request,
        )

    async def _run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            network="xrpl:1",
            asset="XRP:native",
            base_transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://merchant.example",
        ) as client:
            first = await client.post("/metered", content=request_body)
            assert first.status_code == 200
            second = await client.post("/metered", content=request_body)
            assert second.status_code == 200
            assert second.json() == {"ok": True}

    asyncio.run(_run())


def test_httpx_transport_can_close_active_session_explicitly() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )
    open_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="session-ref",
        challengeId=challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        sessionToken="session-token",
        settlementStatus="session_open",
        asset={"code": "XRP"},
        amount={"value": "250", "unit": "drops", "asset": {"code": "XRP"}, "drops": 250},
        availableBalance="750",
        prepaidTotal="1000",
        spentTotal="250",
        lastAction="open",
    )
    close_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:01:00Z",
        reference="session-ref",
        challengeId=challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        settlementStatus="session_closed",
        asset={"code": "XRP"},
        availableBalance="750",
        prepaidTotal="1000",
        spentTotal="250",
        lastAction="close",
    )

    attempts = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["value"] += 1
        authorization = request.headers.get(AUTHORIZATION_HEADER)
        if attempts["value"] == 1:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        if attempts["value"] == 2:
            assert authorization is not None
            return httpx.Response(
                200,
                headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(open_receipt)},
                json={"ok": True},
                request=request,
            )
        if attempts["value"] == 3:
            assert request.method == "GET"
            assert request.headers.get("X-MPP-Session-Id") == "A" * 64
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        assert request.method == "GET"
        assert authorization is not None
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(close_receipt)},
            request=request,
        )

    async def _run() -> None:
        transport = XRPLPaymentTransport(
            signer,
            network="xrpl:1",
            asset="XRP:native",
            base_transport=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://merchant.example",
        ) as client:
            response = await client.get("/metered")
            assert response.status_code == 200
            close_response = await transport.close_session("https://merchant.example/metered")
            assert close_response.status_code == 200
            with pytest.raises(ValueError, match="No active MPP session"):
                await transport.close_session("https://merchant.example/metered")

    asyncio.run(_run())


def test_close_session_requires_method_when_multiple_sessions_share_a_url() -> None:
    transport = XRPLPaymentTransport(
        XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)
    )
    url = "https://merchant.example/metered"
    transport._sessions[transport._session_key_from_url(url, method="GET")] = ClientSessionState(
        session_id="A" * 64,
        session_token="session-token",
        request_method="GET",
    )
    transport._sessions[transport._session_key_from_url(url, method="POST")] = ClientSessionState(
        session_id="B" * 64,
        session_token="session-token",
        request_method="POST",
    )

    async def _run() -> None:
        with pytest.raises(ValueError, match="specify method"):
            await transport.close_session(url)

    asyncio.run(_run())


def test_session_top_up_rejects_network_mismatch() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:0",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )

    with pytest.raises(ValueError, match="does not match signer network"):
        signer.build_session_top_up_credential(challenge, session_token="session-token")


def test_existing_session_prefers_matching_bound_session_option() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    matching_challenge = build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )
    sibling_challenge = build_payment_challenge(
        secret=SECRET,
        realm=REALM,
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="500",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="500",
                minPrepayAmount="2000",
            ),
        ),
        expires_in_seconds=300,
    )
    receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="session-ref",
        challengeId=matching_challenge.id,
        intent="session",
        network="xrpl:1",
        payer=signer.wallet.classic_address,
        recipient=DESTINATION,
        sessionId="A" * 64,
        settlementStatus="session_active",
        asset={"code": "XRP"},
        amount={"value": "250", "unit": "drops", "asset": {"code": "XRP"}, "drops": 250},
        availableBalance="500",
        prepaidTotal="1000",
        spentTotal="500",
        lastAction="use",
    )
    selected: dict[str, str] = {}

    async def fake_build_session_use_credential_async(challenge, *, session_token: str):
        request = decode_challenge_request(challenge)
        selected["session_token"] = session_token
        selected["amount"] = request.amount
        selected["min_prepay_amount"] = request.method_details.min_prepay_amount
        return PaymentCredential(
            challenge=challenge,
            payload={"action": "use", "sessionToken": session_token},
        )

    signer.build_session_use_credential_async = fake_build_session_use_credential_async  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={
                    WWW_AUTHENTICATE_HEADER: ", ".join(
                        [
                            render_payment_challenge(sibling_challenge),
                            render_payment_challenge(matching_challenge),
                        ]
                    )
                },
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            json={"ok": True},
            request=request,
        )

    async def _run() -> httpx.Response:
        transport = XRPLPaymentTransport(
            signer,
            network="xrpl:1",
            asset="XRP:native",
            base_transport=httpx.MockTransport(handler),
        )
        transport._sessions[
            transport._session_key_from_url("https://merchant.example/metered", method="GET")
        ] = ClientSessionState(
            session_id="A" * 64,
            session_token="session-token",
            request_method="GET",
            asset_identifier="XRP:native",
            amount="250",
            min_prepay_amount="1000",
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://merchant.example",
        ) as client:
            return await client.get("/metered")

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert selected == {
        "session_token": "session-token",
        "amount": "250",
        "min_prepay_amount": "1000",
    }
