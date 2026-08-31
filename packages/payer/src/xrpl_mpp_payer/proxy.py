from __future__ import annotations

import threading
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
import uvicorn

from xrpl_mpp_payer.payer import XRPLPayer, build_signer_from_env
from xrpl_mpp_payer.receipts import ReceiptStore

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def create_proxy_app(
    *,
    target_base_url: str,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
    intent: str | None = None,
    channel_id: str | None = None,
    cumulative_amount: str = "0",
    open_transaction: str | None = None,
    channel_funding_amount: str | None = None,
    expected_recipient: str | None = None,
    transport=None,
    store: ReceiptStore | None = None,
    payer: XRPLPayer | None = None,
) -> Starlette:
    active_payer = payer or XRPLPayer(
        None if dry_run else build_signer_from_env(),
        store=store,
        expected_recipient=expected_recipient,
    )
    normalized_target = target_base_url.rstrip("/")

    async def proxy(request: Request) -> Response:
        path = request.path_params.get("path", "")
        target_url = normalized_target
        if path:
            target_url = f"{target_url}/{path.lstrip('/')}"
        if request.url.query:
            target_url = f"{target_url}?{request.url.query}"

        body = await request.body()
        forwarded_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        result = await active_payer.pay(
            url=target_url,
            method=request.method,
            headers=forwarded_headers,
            content=body or None,
            amount=amount,
            asset=asset,
            issuer=issuer,
            max_spend=max_spend,
            dry_run=dry_run,
            intent=intent,
            channel_id=channel_id,
            cumulative_amount=cumulative_amount,
            open_transaction=open_transaction,
            channel_funding_amount=channel_funding_amount,
            expected_recipient=expected_recipient,
            transport=transport,
        )
        response_headers = {
            key: value
            for key, value in result.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=response_headers,
        )

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                endpoint=proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            )
        ]
    )


def run_proxy(
    *,
    target_base_url: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
    intent: str | None = None,
    channel_id: str | None = None,
    cumulative_amount: str = "0",
    open_transaction: str | None = None,
    channel_funding_amount: str | None = None,
    expected_recipient: str | None = None,
) -> None:
    app = create_proxy_app(
        target_base_url=target_base_url,
        amount=amount,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
        intent=intent,
        channel_id=channel_id,
        cumulative_amount=cumulative_amount,
        open_transaction=open_transaction,
        channel_funding_amount=channel_funding_amount,
        expected_recipient=expected_recipient,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


class ProxyManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._bind_url: str | None = None
        self._config_signature: tuple[object, ...] | None = None

    def start(
        self,
        *,
        target_base_url: str,
        host: str = "127.0.0.1",
        port: int = 8787,
        amount: float = 0.001,
        asset: str = "XRP",
        issuer: str | None = None,
        max_spend: float | None = None,
        dry_run: bool = False,
        intent: str | None = None,
        channel_id: str | None = None,
        cumulative_amount: str = "0",
        open_transaction: str | None = None,
        channel_funding_amount: str | None = None,
        expected_recipient: str | None = None,
    ) -> str:
        with self._lock:
            normalized_target_base_url = target_base_url.rstrip("/")
            bind_url = f"http://{host}:{port}"
            config_signature = (
                bind_url,
                normalized_target_base_url,
                amount,
                asset,
                issuer,
                max_spend,
                dry_run,
                intent,
                channel_id,
                cumulative_amount,
                open_transaction,
                channel_funding_amount,
                expected_recipient,
            )
            if self._server is not None:
                if self._config_signature == config_signature:
                    return bind_url
                raise RuntimeError(
                    "Proxy is already running with a different configuration. Restart the MCP server to change it."
                )

            app = create_proxy_app(
                target_base_url=normalized_target_base_url,
                amount=amount,
                asset=asset,
                issuer=issuer,
                max_spend=max_spend,
                dry_run=dry_run,
                intent=intent,
                channel_id=channel_id,
                cumulative_amount=cumulative_amount,
                open_transaction=open_transaction,
                channel_funding_amount=channel_funding_amount,
                expected_recipient=expected_recipient,
            )
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()

            for _ in range(100):
                if getattr(server, "started", False):
                    self._server = server
                    self._thread = thread
                    self._bind_url = bind_url
                    self._config_signature = config_signature
                    return bind_url
                time.sleep(0.05)

            raise RuntimeError("Proxy server failed to start")


proxy_manager = ProxyManager()
