from xrpl_mpp_core import MPPProblemDetails


class XRPLMPPMiddlewareError(Exception):
    """Base exception for xrpl_mpp_middleware errors."""


class RouteConfigurationError(XRPLMPPMiddlewareError):
    """Raised when middleware route configuration is invalid."""


class InvalidPaymentHeaderError(XRPLMPPMiddlewareError):
    """Raised when a payment header cannot be decoded or validated."""


class FacilitatorError(XRPLMPPMiddlewareError):
    """Base exception for facilitator client failures."""


class FacilitatorTransportError(FacilitatorError):
    """Raised when the facilitator cannot be reached or returns 5xx."""

    def __init__(self, detail: str, *, payment_reference: str | None = None) -> None:
        self.detail = detail
        self.payment_reference = payment_reference
        super().__init__(detail)


class FacilitatorSettlementPendingError(FacilitatorTransportError):
    """A sanitized facilitator response for an ambiguously settled payment."""

    def __init__(
        self,
        problem: MPPProblemDetails,
        *,
        retry_after: str | None = None,
    ) -> None:
        self.problem = problem
        self.retry_after = retry_after
        super().__init__(
            problem.detail,
            payment_reference=problem.payment_reference,
        )


class FacilitatorProtocolError(FacilitatorError):
    """Raised when the facilitator returns an unexpected response shape."""


class FacilitatorPaymentError(FacilitatorError):
    """Raised when the facilitator rejects a payment attempt."""

    def __init__(self, stage: str, status_code: int, detail: str) -> None:
        self.stage = stage
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{stage} failed with {status_code}: {detail}")
