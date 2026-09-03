from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def allow_insecure_loopback_facilitator(url: str) -> bool:
    """Opt into plaintext only for a literal localhost/loopback origin."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or hostname is None:
        return False
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False
