from __future__ import annotations

from xrpl_mpp_core.xrpl import ClassicAddress, XRPLModel, XRPLNetwork


class XRPLDID(XRPLModel):
    """Parsed ``did:pkh:xrpl`` credential source."""

    network: XRPLNetwork
    address: ClassicAddress

    def __str__(self) -> str:
        return f"did:pkh:xrpl:{self.network}:{self.address}"


def build_xrpl_did(*, network: XRPLNetwork, address: str) -> str:
    """Build a validated XRPL payer DID for an MPP credential."""

    return str(XRPLDID(network=network, address=address))


def parse_xrpl_did(source: str, *, expected_network: XRPLNetwork | None = None) -> XRPLDID:
    """Parse and validate ``did:pkh:xrpl:{network}:{classic-address}``."""

    if not isinstance(source, str) or not source:
        raise ValueError("credential source is required")
    parts = source.split(":")
    if len(parts) != 5 or parts[:3] != ["did", "pkh", "xrpl"]:
        raise ValueError(
            "credential source must use did:pkh:xrpl:{network}:{address}"
        )

    parsed = XRPLDID(network=parts[3], address=parts[4])
    if expected_network is not None and parsed.network != expected_network:
        raise ValueError(
            f"credential source network {parsed.network} does not match {expected_network}"
        )
    return parsed


def classic_address_from_did(
    source: str,
    *,
    expected_network: XRPLNetwork | None = None,
) -> str:
    """Extract the payer address while retaining optional network binding."""

    return parse_xrpl_did(source, expected_network=expected_network).address
