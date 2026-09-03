from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import math
import time
from typing import Any, Literal

from xrpl.models import PathStep
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency as LedgerIssuedCurrency
from xrpl.models.currencies import XRP as LedgerXRP
from xrpl.models.requests import AccountInfo, RipplePathFind

from xrpl_mpp_core import (
    IssuedCurrency,
    MPToken,
    parse_currency,
    serialize_currency,
)

MAX_SLIPPAGE_BPS = 1_000
MAX_PATHFIND_RETRIES = 3
MAX_PATHFIND_BACKOFF_SECONDS = 7.0
MAX_PATH_ALTERNATIVES = 16
MAX_PATHS = 6
MAX_PATH_STEPS = 8
TRANSFER_RATE_SCALE = 1_000_000_000
MAX_TRANSFER_RATE = 2_000_000_000


class XRPLPathfindingError(ValueError):
    """Raised before signing when an IOU route exceeds local authorization."""


@dataclass(frozen=True, init=False, slots=True)
class XRPLIOUPathfindingPolicy:
    """Explicit source-side authorization for IOU payments.

    ``source_currency`` is the only asset that path finding may spend.
    ``max_source_amount`` is an absolute source-side ceiling (drops for XRP,
    decimal units for an issued currency), independent of the destination-side
    MPP payment policy.
    """

    source_currency: str
    max_source_amount: Decimal
    slippage_bps: int
    retry_delays_seconds: tuple[float, ...]

    def __init__(
        self,
        *,
        source_currency: str,
        max_source_amount: str,
        slippage_bps: int = 50,
        retry_delays_seconds: tuple[float, ...] = (1.0, 2.0, 4.0),
    ) -> None:
        parsed_source = parse_currency(source_currency)
        if isinstance(parsed_source, MPToken):
            raise ValueError("MPT assets do not support XRPL path finding")
        canonical_source = serialize_currency(parsed_source)
        if not isinstance(max_source_amount, str):
            raise TypeError("max_source_amount must be a positive decimal string")
        try:
            ceiling = Decimal(max_source_amount)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("max_source_amount must be a positive decimal string") from exc
        if not ceiling.is_finite() or ceiling <= 0:
            raise ValueError("max_source_amount must be a positive finite decimal string")
        if parsed_source == "XRP" and (
            not isinstance(max_source_amount, str)
            or not max_source_amount.isascii()
            or not max_source_amount.isdigit()
        ):
            raise ValueError("XRP max_source_amount must be a positive drops string")
        if (
            isinstance(slippage_bps, bool)
            or not isinstance(slippage_bps, int)
            or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS
        ):
            raise ValueError("slippage_bps must be an integer between 0 and 1000")
        if not isinstance(retry_delays_seconds, tuple):
            raise TypeError("retry_delays_seconds must be a tuple")
        if len(retry_delays_seconds) > MAX_PATHFIND_RETRIES:
            raise ValueError("retry_delays_seconds permits at most three retries")
        if any(
            isinstance(delay, bool)
            or not isinstance(delay, int | float)
            or not math.isfinite(delay)
            or delay < 0
            for delay in retry_delays_seconds
        ):
            raise ValueError("retry delays must be non-negative numbers")
        if sum(retry_delays_seconds) > MAX_PATHFIND_BACKOFF_SECONDS:
            raise ValueError("path-finding retry backoff cannot exceed seven seconds")

        object.__setattr__(self, "source_currency", canonical_source)
        object.__setattr__(self, "max_source_amount", ceiling)
        object.__setattr__(self, "slippage_bps", slippage_bps)
        object.__setattr__(self, "retry_delays_seconds", retry_delays_seconds)


@dataclass(frozen=True, slots=True)
class ResolvedIOUPayment:
    send_max: str | IssuedCurrencyAmount
    paths: list[list[PathStep]] | None
    strategy: Literal["direct", "cross-currency"]


def resolve_iou_payment(
    *,
    client: Any,
    sender: str,
    recipient: str,
    destination_amount: IssuedCurrencyAmount,
    policy: XRPLIOUPathfindingPolicy,
) -> ResolvedIOUPayment:
    """Resolve a bounded IOU route without delegating spend policy to the RPC."""

    source = parse_currency(policy.source_currency)
    destination_currency = destination_amount.currency
    destination_issuer = destination_amount.issuer
    destination_value = _positive_decimal(
        destination_amount.value,
        name="destination amount",
    )

    if isinstance(source, IssuedCurrency) and (
        source.currency == destination_currency and source.issuer == destination_issuer
    ):
        transfer_rate = (
            TRANSFER_RATE_SCALE
            if recipient == destination_issuer
            else _read_transfer_rate(client, destination_issuer)
        )
        with localcontext() as context:
            context.prec = 100
            quoted = (
                destination_value
                * Decimal(transfer_rate)
                / Decimal(TRANSFER_RATE_SCALE)
            )
        buffered = _apply_decimal_slippage(quoted, policy.slippage_bps)
        send_max_value = _format_issued_value_ceiling(buffered)
        _enforce_source_ceiling(Decimal(send_max_value), policy)
        return ResolvedIOUPayment(
            send_max=IssuedCurrencyAmount(
                currency=source.currency,
                issuer=source.issuer,
                value=send_max_value,
            ),
            paths=None,
            strategy="direct",
        )

    alternatives = _find_paths_with_retry(
        client=client,
        sender=sender,
        recipient=recipient,
        destination_amount=destination_amount,
        source=source,
        retry_delays_seconds=policy.retry_delays_seconds,
    )
    resolved = [
        _parse_alternative(alternative, source=source, policy=policy)
        for alternative in alternatives
    ]
    chosen = min(resolved, key=lambda candidate: candidate[0])
    _, send_max, paths = chosen
    return ResolvedIOUPayment(
        send_max=send_max,
        paths=paths,
        strategy="cross-currency",
    )


def _read_transfer_rate(client: Any, issuer: str) -> int:
    response = client.request(AccountInfo(account=issuer, ledger_index="validated"))
    result = getattr(response, "result", None)
    account_data = result.get("account_data") if isinstance(result, dict) else None
    if not isinstance(account_data, dict):
        raise XRPLPathfindingError("XRPL account_info returned no issuer account data")
    raw_rate = account_data.get("TransferRate")
    if raw_rate is None or raw_rate == 0 or raw_rate == "0":
        return TRANSFER_RATE_SCALE
    if isinstance(raw_rate, bool):
        raise XRPLPathfindingError("Issuer TransferRate is invalid")
    try:
        rate = int(raw_rate)
    except (TypeError, ValueError) as exc:
        raise XRPLPathfindingError("Issuer TransferRate is invalid") from exc
    if not TRANSFER_RATE_SCALE <= rate <= MAX_TRANSFER_RATE:
        raise XRPLPathfindingError("Issuer TransferRate is outside the XRPL range")
    return rate


def _find_paths_with_retry(
    *,
    client: Any,
    sender: str,
    recipient: str,
    destination_amount: IssuedCurrencyAmount,
    source: Literal["XRP"] | IssuedCurrency,
    retry_delays_seconds: tuple[float, ...],
) -> list[dict[str, Any]]:
    source_model = (
        LedgerXRP()
        if source == "XRP"
        else LedgerIssuedCurrency(currency=source.currency, issuer=source.issuer)
    )
    request = RipplePathFind(
        source_account=sender,
        destination_account=recipient,
        destination_amount=destination_amount,
        source_currencies=[source_model],
        ledger_index="validated",
    )
    last_error: Exception | None = None
    for attempt in range(len(retry_delays_seconds) + 1):
        if attempt:
            time.sleep(retry_delays_seconds[attempt - 1])
        try:
            response = client.request(request)
        except Exception as exc:  # pragma: no cover - concrete clients vary
            last_error = exc
            continue
        result = getattr(response, "result", None)
        if not isinstance(result, dict):
            raise XRPLPathfindingError("ripple_path_find returned an invalid result")
        alternatives = result.get("alternatives")
        if not isinstance(alternatives, list):
            raise XRPLPathfindingError("ripple_path_find returned invalid alternatives")
        if len(alternatives) > MAX_PATH_ALTERNATIVES:
            raise XRPLPathfindingError("ripple_path_find returned too many alternatives")
        if alternatives:
            if not all(isinstance(value, dict) for value in alternatives):
                raise XRPLPathfindingError("ripple_path_find returned an invalid alternative")
            return alternatives
        last_error = None
    if last_error is not None:
        raise XRPLPathfindingError("ripple_path_find failed") from last_error
    raise XRPLPathfindingError("No authorized XRPL payment path is available")


def _parse_alternative(
    alternative: dict[str, Any],
    *,
    source: Literal["XRP"] | IssuedCurrency,
    policy: XRPLIOUPathfindingPolicy,
) -> tuple[Decimal, str | IssuedCurrencyAmount, list[list[PathStep]] | None]:
    raw_source_amount = alternative.get("source_amount")
    if source == "XRP":
        if (
            not isinstance(raw_source_amount, str)
            or not raw_source_amount.isascii()
            or not raw_source_amount.isdigit()
            or int(raw_source_amount) <= 0
        ):
            raise XRPLPathfindingError("Path quote does not use authorized XRP drops")
        quoted = Decimal(raw_source_amount)
        numerator = int(raw_source_amount) * (10_000 + policy.slippage_bps)
        buffered_drops = (numerator + 9_999) // 10_000
        buffered = Decimal(buffered_drops)
        _enforce_source_ceiling(buffered, policy)
        send_max: str | IssuedCurrencyAmount = str(buffered_drops)
    else:
        if not isinstance(raw_source_amount, dict) or set(raw_source_amount) != {
            "currency",
            "issuer",
            "value",
        }:
            raise XRPLPathfindingError(
                "Path quote does not use the authorized issued source currency"
            )
        if (
            raw_source_amount.get("currency") != source.currency
            or raw_source_amount.get("issuer") != source.issuer
        ):
            raise XRPLPathfindingError(
                "Path quote changed the authorized source currency"
            )
        quoted = _positive_decimal(raw_source_amount.get("value"), name="path quote")
        buffered = _apply_decimal_slippage(quoted, policy.slippage_bps)
        buffered_value = _format_issued_value_ceiling(buffered)
        _enforce_source_ceiling(Decimal(buffered_value), policy)
        send_max = IssuedCurrencyAmount(
            currency=source.currency,
            issuer=source.issuer,
            value=buffered_value,
        )

    paths = _parse_paths(alternative.get("paths_computed"))
    return quoted, send_max, paths


def _parse_paths(raw_paths: Any) -> list[list[PathStep]] | None:
    if not isinstance(raw_paths, list):
        raise XRPLPathfindingError("Path quote is missing paths_computed")
    if not raw_paths:
        return None
    if not 1 <= len(raw_paths) <= MAX_PATHS:
        raise XRPLPathfindingError("Path quote exceeds the XRPL path-count limit")
    paths: list[list[PathStep]] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, list) or not 1 <= len(raw_path) <= MAX_PATH_STEPS:
            raise XRPLPathfindingError("Path quote exceeds the XRPL path-step limit")
        path: list[PathStep] = []
        for raw_step in raw_path:
            if not isinstance(raw_step, dict):
                raise XRPLPathfindingError("Path quote contains an invalid path step")
            semantic = {
                key: raw_step[key]
                for key in ("account", "currency", "issuer")
                if key in raw_step
            }
            if not semantic or any(not isinstance(value, str) for value in semantic.values()):
                raise XRPLPathfindingError("Path quote contains an empty path step")
            try:
                path.append(PathStep(**semantic))
            except Exception as exc:
                raise XRPLPathfindingError("Path quote contains an invalid path step") from exc
        paths.append(path)
    return paths


def _positive_decimal(value: Any, *, name: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise XRPLPathfindingError(f"{name} must be a positive decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise XRPLPathfindingError(f"{name} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise XRPLPathfindingError(f"{name} must be a positive finite decimal")
    return parsed


def _apply_decimal_slippage(value: Decimal, slippage_bps: int) -> Decimal:
    with localcontext() as context:
        context.prec = 100
        return value * Decimal(10_000 + slippage_bps) / Decimal(10_000)


def _format_issued_value_ceiling(value: Decimal) -> str:
    try:
        with localcontext() as context:
            context.prec = 100
            quantum = Decimal(1).scaleb(value.adjusted() - 15)
            rounded = value.quantize(quantum, rounding=ROUND_CEILING)
    except InvalidOperation as exc:
        raise XRPLPathfindingError("Path quote is outside the XRPL amount range") from exc
    rendered = format(rounded, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _enforce_source_ceiling(
    buffered_amount: Decimal,
    policy: XRPLIOUPathfindingPolicy,
) -> None:
    if buffered_amount > policy.max_source_amount:
        raise XRPLPathfindingError(
            f"Authorized XRPL source-spend maximum is {policy.max_source_amount}; "
            f"route requires {buffered_amount}"
        )
