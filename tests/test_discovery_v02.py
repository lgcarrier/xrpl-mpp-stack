from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError
import pytest

from xrpl_mpp_middleware.discovery import (
    DiscoveryConfigurationError,
    DiscoveryDocumentError,
    PayableRoute,
    PaymentOffer,
    ServiceDocs,
    ServiceInfo,
    augment_openapi,
    augment_openapi_from_route_configs,
    parse_route_key,
)
from xrpl_mpp_middleware.types import (
    ChargeRouteSpec,
    RouteConfig,
    SessionRouteSpec,
)


ISSUER = "rPEPPER7kfTD9w2To4CQk6UCfuHM9c6GDY"


def _openapi_document() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "MPP API", "version": "2.7.1", "x-owner": "merchant"},
        "paths": {
            "/quote": {
                "parameters": [{"name": "locale", "in": "query"}],
                "post": {
                    "summary": "Create quote",
                    "x-existing": {"keep": True},
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"prompt": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Quote",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Quote"}
                                }
                            },
                        }
                    },
                },
            },
            "/stream": {
                "get": {
                    "operationId": "stream",
                    "responses": {"200": {"description": "Stream"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Quote": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
        "x-unrelated": ["preserve", "me"],
    }


def test_augment_openapi_emits_service_and_payment_extensions_without_mutation() -> None:
    document = _openapi_document()
    original = deepcopy(document)
    request_body = deepcopy(document["paths"]["/quote"]["post"]["requestBody"])

    augmented = augment_openapi(
        document,
        routes=[
            PayableRoute(
                path="/quote",
                method="POST",
                offers=(
                    PaymentOffer(
                        intent="charge",
                        method="xrpl",
                        amount="250",
                        currency="XRP",
                        description="One quote",
                    ),
                ),
            )
        ],
        service_info=ServiceInfo(
            categories=("developer-tools", "data"),
            docs=ServiceDocs(
                apiReference="https://api.example.test/reference",
                homepage="https://example.test",
                llms="https://example.test/llms.txt",
            ),
        ),
    )

    assert document == original
    assert augmented is not document
    assert augmented["x-service-info"] == {
        "categories": ["developer-tools", "data"],
        "docs": {
            "apiReference": "https://api.example.test/reference",
            "homepage": "https://example.test",
            "llms": "https://example.test/llms.txt",
        },
    }
    operation = augmented["paths"]["/quote"]["post"]
    assert operation["x-payment-info"] == {
        "offers": [
            {
                "intent": "charge",
                "method": "xrpl",
                "amount": "250",
                "currency": "XRP",
                "description": "One quote",
            }
        ]
    }
    assert operation["responses"]["402"] == {"description": "Payment Required"}
    assert operation["requestBody"] == request_body
    assert operation["responses"]["200"] == original["paths"]["/quote"]["post"][
        "responses"
    ]["200"]
    assert augmented["components"] == original["components"]
    assert augmented["x-unrelated"] == original["x-unrelated"]


def test_route_config_generation_covers_charge_and_session_multi_offer_route() -> None:
    issued_currency = f'{{"currency":"USD","issuer":"{ISSUER}"}}'
    route_config = RouteConfig(
        facilitatorUrl="https://facilitator.example.test",
        bearerToken="secret",
        description="Default route price",
        chargeOptions=[
            ChargeRouteSpec(
                network="testnet",
                recipient=ISSUER,
                currency="XRP",
                amount="25",
            ),
            ChargeRouteSpec(
                network="testnet",
                recipient=ISSUER,
                currency=issued_currency,
                amount="1.25",
                description="Issued-token price",
            ),
        ],
        sessionOptions=[
            SessionRouteSpec(
                network="testnet",
                recipient=ISSUER,
                amount="5",
                description="Per streamed unit",
            )
        ],
    )

    augmented = augment_openapi_from_route_configs(
        _openapi_document(),
        route_configs={"POST /quote": route_config},
        service_info={"categories": ["compute"]},
    )

    assert augmented["paths"]["/quote"]["post"]["x-payment-info"] == {
        "offers": [
            {
                "intent": "charge",
                "method": "xrpl",
                "amount": "25",
                "currency": "XRP",
                "description": "Default route price",
            },
            {
                "intent": "charge",
                "method": "xrpl",
                "amount": None,
                "currency": issued_currency,
                "description": "Issued-token price",
            },
            {
                "intent": "session",
                "method": "xrpl",
                "amount": "5",
                "currency": "XRP",
                "description": "Per streamed unit",
            },
        ]
    }


def test_dynamic_offer_keeps_required_null_amount_and_omits_optional_nulls() -> None:
    augmented = augment_openapi(
        _openapi_document(),
        routes=[
            {
                "path": "/stream",
                "method": "get",
                "offers": [
                    {
                        "intent": "session",
                        "method": "xrpl",
                        "amount": None,
                    }
                ],
            }
        ],
    )

    assert augmented["paths"]["/stream"]["get"]["x-payment-info"] == {
        "offers": [{"intent": "session", "method": "xrpl", "amount": None}]
    }


def test_existing_valid_402_response_and_other_operation_fields_are_preserved() -> None:
    document = _openapi_document()
    custom_402 = {
        "description": "MPP challenge",
        "headers": {"WWW-Authenticate": {"schema": {"type": "string"}}},
    }
    operation = document["paths"]["/quote"]["post"]
    operation["responses"]["402"] = deepcopy(custom_402)
    operation["x-payment-info"] = {"intent": "charge", "method": "stale"}

    augmented = augment_openapi(
        document,
        routes=[
            PayableRoute(
                path="/quote",
                method="post",
                offers=(PaymentOffer(intent="charge", method="xrpl", amount="1"),),
            )
        ],
    )

    updated = augmented["paths"]["/quote"]["post"]
    assert updated["responses"]["402"] == custom_402
    assert updated["x-payment-info"] == {
        "offers": [{"intent": "charge", "method": "xrpl", "amount": "1"}]
    }
    assert updated["summary"] == "Create quote"
    assert updated["x-existing"] == {"keep": True}


def test_route_input_order_does_not_change_augmented_document() -> None:
    quote = PayableRoute(
        path="/quote",
        method="post",
        offers=(PaymentOffer(intent="charge", method="xrpl", amount="2"),),
    )
    stream = PayableRoute(
        path="/stream",
        method="get",
        offers=(PaymentOffer(intent="session", method="xrpl", amount="3"),),
    )

    forward = augment_openapi(_openapi_document(), routes=[quote, stream])
    reverse = augment_openapi(_openapi_document(), routes=[stream, quote])

    assert forward == reverse


@pytest.mark.parametrize("intent", ["subscription", "authorize", "refund"])
def test_offer_rejects_intents_not_supported_by_discovery_draft_01(intent: str) -> None:
    with pytest.raises(ValidationError, match="Input should be 'charge' or 'session'"):
        PaymentOffer(intent=intent, method="xrpl", amount="1")  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", ["", "00", "01", "-1", "+1", "1.0", " 1"])
def test_offer_rejects_noncanonical_amounts(amount: str) -> None:
    with pytest.raises(ValidationError, match="canonical non-negative integer"):
        PaymentOffer(intent="charge", method="xrpl", amount=amount)


def test_offer_accepts_zero_and_dynamic_amounts() -> None:
    assert PaymentOffer(intent="charge", method="xrpl", amount="0").amount == "0"
    assert PaymentOffer(intent="session", method="xrpl", amount=None).amount is None


@pytest.mark.parametrize("method", ["XRPL", "xrpl-2", "xrpl2", "xrpl "])
def test_offer_rejects_nonstandard_payment_method_identifiers(method: str) -> None:
    with pytest.raises(ValidationError, match="lowercase ASCII letters"):
        PaymentOffer(intent="charge", method=method, amount="1")


def test_parse_route_key_accepts_string_and_tuple_forms() -> None:
    assert parse_route_key("POST /quote") == ("post", "/quote")
    assert parse_route_key(("GET", "/stream")) == ("get", "/stream")


@pytest.mark.parametrize(
    ("route_key", "message"),
    [
        ("/quote", "METHOD /path"),
        ("CONNECT /quote", "unsupported OpenAPI"),
        (("POST", "quote"), "absolute HTTP path"),
    ],
)
def test_parse_route_key_rejects_invalid_operation_keys(route_key, message: str) -> None:
    with pytest.raises(DiscoveryConfigurationError, match=message):
        parse_route_key(route_key)


def test_invalid_service_documentation_uri_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absolute RFC 3986 URI"):
        ServiceDocs(homepage="/relative")
    with pytest.raises(ValidationError, match="invalid percent escape"):
        ServiceDocs(homepage="https://example.test/bad%2")


def test_augment_accepts_openapi_30_documents() -> None:
    document = _openapi_document()
    document["openapi"] = "3.0.3"

    assert augment_openapi(document, routes=[])["openapi"] == "3.0.3"


def test_augment_rejects_non_openapi_3_documents() -> None:
    document = _openapi_document()
    document["openapi"] = "2.0"

    with pytest.raises(DiscoveryDocumentError, match="OpenAPI 3.x"):
        augment_openapi(document, routes=[])


def test_augment_rejects_missing_runtime_operation_instead_of_inventing_it() -> None:
    route = PayableRoute(
        path="/missing",
        method="post",
        offers=(PaymentOffer(intent="charge", method="xrpl", amount="1"),),
    )

    with pytest.raises(DiscoveryDocumentError, match="no path item"):
        augment_openapi(_openapi_document(), routes=[route])


def test_augment_rejects_duplicate_payable_operation_metadata() -> None:
    route = PayableRoute(
        path="/quote",
        method="post",
        offers=(PaymentOffer(intent="charge", method="xrpl", amount="1"),),
    )

    with pytest.raises(DiscoveryConfigurationError, match="duplicate payable"):
        augment_openapi(_openapi_document(), routes=[route, route])


def test_augment_rejects_invalid_existing_402_response() -> None:
    document = _openapi_document()
    document["paths"]["/quote"]["post"]["responses"]["402"] = {}
    route = PayableRoute(
        path="/quote",
        method="post",
        offers=(PaymentOffer(intent="charge", method="xrpl", amount="1"),),
    )

    with pytest.raises(DiscoveryDocumentError, match="needs description or \\$ref"):
        augment_openapi(document, routes=[route])
