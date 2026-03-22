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


class FacilitatorProtocolError(FacilitatorError):
    """Raised when the facilitator returns an unexpected response shape."""


class FacilitatorPaymentError(FacilitatorError):
    """Raised when the facilitator rejects a payment attempt."""

    def __init__(self, stage: str, status_code: int, detail: str) -> None:
        self.stage = stage
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{stage} failed with {status_code}: {detail}")
