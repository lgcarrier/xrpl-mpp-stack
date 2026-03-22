from __future__ import annotations

import httpx

from xrpl_mpp_core import (
    FacilitatorSupportedResponse,
    PaymentCredential,
    PaymentReceipt,
)
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    FacilitatorTransportError,
)


class XRPLFacilitatorClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout: float = 10.0,
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._timeout = timeout
        self._async_client = async_client
        self._owns_client = async_client is None
        self._supported_cache: FacilitatorSupportedResponse | None = None

    async def startup(self) -> None:
        await self.get_supported(force_refresh=False)

    async def aclose(self) -> None:
        if self._async_client is not None and self._owns_client:
            await self._async_client.aclose()

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupportedResponse:
        if self._supported_cache is not None and not force_refresh:
            return self._supported_cache

        response = await self._request("GET", "/supported")
        supported = FacilitatorSupportedResponse.model_validate(response)
        self._supported_cache = supported
        return supported

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        response = await self._request(
            "POST",
            "/charge",
            json={"credential": credential.model_dump(by_alias=True, exclude_none=True)},
            authenticated=True,
            stage="charge",
        )
        return PaymentReceipt.model_validate(response)

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        response = await self._request(
            "POST",
            "/session",
            json={"credential": credential.model_dump(by_alias=True, exclude_none=True)},
            authenticated=True,
            stage="session",
        )
        return PaymentReceipt.model_validate(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        authenticated: bool = False,
        stage: str = "request",
    ) -> dict[str, object]:
        client = self._async_client
        if client is None:
            client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
            self._async_client = client

        headers = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        try:
            response = await client.request(method, path, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise FacilitatorTransportError("Facilitator request timed out") from exc
        except httpx.HTTPError as exc:
            raise FacilitatorTransportError("Unable to reach facilitator") from exc

        if response.status_code >= 500:
            raise FacilitatorTransportError("Facilitator is unavailable")

        if response.status_code == 401:
            raise FacilitatorProtocolError(
                f"Facilitator authentication failed: {self._extract_detail(response)}"
            )

        if response.status_code == 402:
            raise FacilitatorPaymentError(stage, response.status_code, self._extract_detail(response))

        if response.status_code >= 400:
            raise FacilitatorProtocolError(
                f"Facilitator returned unexpected status {response.status_code}: "
                f"{self._extract_detail(response)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FacilitatorProtocolError("Facilitator returned invalid JSON") from exc

        if not isinstance(body, dict):
            raise FacilitatorProtocolError("Facilitator returned a non-object JSON response")
        return body

    @staticmethod
    def _extract_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text.strip() or "unknown facilitator error"

        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        return response.text.strip() or "unknown facilitator error"
