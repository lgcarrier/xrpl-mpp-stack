from functools import lru_cache
import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from xrpl_mpp_core import XRPLNetwork, clean_env_value


class Settings(BaseSettings):
    GATEWAY_AUTH_MODE: Literal["single_token", "redis_gateways"] = "single_token"
    XRPL_RPC_URL: str = "https://s1.ripple.com:51234"
    ALLOW_INSECURE_XRPL_RPC: bool = False
    MY_DESTINATION_ADDRESS: str
    FACILITATOR_BEARER_TOKEN: SecretStr | None = None
    REDIS_URL: SecretStr
    NETWORK_ID: XRPLNetwork = "mainnet"
    SETTLEMENT_MODE: Literal["validated"] = "validated"
    VALIDATION_TIMEOUT: int = 15
    MIN_XRP_DROPS: int = 1000
    ALLOWED_ISSUED_ASSETS: str = ""
    ALLOWED_MPT_ISSUANCE_IDS: str = ""
    ENABLE_API_DOCS: bool = False
    MAX_REQUEST_BODY_BYTES: int = 32768
    REPLAY_PROCESSED_TTL_SECONDS: int = 604800
    MAX_PAYMENT_LEDGER_WINDOW: int = 20
    MPP_CHALLENGE_SECRET: SecretStr
    MPP_CHALLENGE_PREVIOUS_SECRETS: str = ""
    MPP_CHALLENGE_TTL_SECONDS: int = 300
    MPP_DEFAULT_REALM: str | None = None
    PAYCHANNEL_PAYER_PUBLIC_KEY: SecretStr | None = None
    PAYCHANNEL_RECIPIENT_SEED: SecretStr | None = None
    PAYCHANNEL_MIN_SETTLE_DELAY: int = 3600
    PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS: int = 60
    PAYCHANNEL_MAX_REDEMPTION_FEE_DROPS: int = 1000
    PAYCHANNEL_REDEEM_INTERVAL_SECONDS: int = 0
    PAYCHANNEL_IDLE_CLOSE_SECONDS: int = 0
    PAYCHANNEL_REDEEM_BATCH_SIZE: int = 100
    PAYCHANNEL_REDEEM_LEASE_SECONDS: int = 75

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator(
        "GATEWAY_AUTH_MODE",
        "XRPL_RPC_URL",
        "MY_DESTINATION_ADDRESS",
        "FACILITATOR_BEARER_TOKEN",
        "REDIS_URL",
        "NETWORK_ID",
        "SETTLEMENT_MODE",
        "ALLOWED_ISSUED_ASSETS",
        "ALLOWED_MPT_ISSUANCE_IDS",
        "MPP_CHALLENGE_SECRET",
        "MPP_CHALLENGE_PREVIOUS_SECRETS",
        "MPP_DEFAULT_REALM",
        "PAYCHANNEL_PAYER_PUBLIC_KEY",
        "PAYCHANNEL_RECIPIENT_SEED",
        mode="before",
    )
    @classmethod
    def _clean_string_settings(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str):
            cleaned = clean_env_value(value)
            if cleaned is None and info.field_name in {
                "ALLOWED_ISSUED_ASSETS",
                "ALLOWED_MPT_ISSUANCE_IDS",
                "MPP_CHALLENGE_PREVIOUS_SECRETS",
            }:
                return ""
            return cleaned
        return value

    @field_validator(
        "VALIDATION_TIMEOUT",
        "MAX_REQUEST_BODY_BYTES",
        "REPLAY_PROCESSED_TTL_SECONDS",
        "MAX_PAYMENT_LEDGER_WINDOW",
        "MPP_CHALLENGE_TTL_SECONDS",
        "PAYCHANNEL_MAX_REDEMPTION_FEE_DROPS",
        "PAYCHANNEL_REDEEM_BATCH_SIZE",
        "PAYCHANNEL_REDEEM_LEASE_SECONDS",
    )
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("MIN_XRP_DROPS")
    @classmethod
    def _validate_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be zero or greater")
        return value

    @field_validator(
        "PAYCHANNEL_MIN_SETTLE_DELAY",
        "PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS",
        "PAYCHANNEL_REDEEM_INTERVAL_SECONDS",
        "PAYCHANNEL_IDLE_CLOSE_SECONDS",
    )
    @classmethod
    def _validate_paychannel_settle_delay(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be zero or greater")
        return value

    def gateway_auth_uses_redis(self) -> bool:
        return self.GATEWAY_AUTH_MODE == "redis_gateways"

    @model_validator(mode="after")
    def _validate_auth_settings(self) -> "Settings":
        try:
            rpc_url = urlsplit(self.XRPL_RPC_URL)
            hostname = rpc_url.hostname
            _ = rpc_url.port
        except ValueError as exc:
            raise ValueError("XRPL_RPC_URL is malformed") from exc
        if (
            not hostname
            or rpc_url.username is not None
            or rpc_url.password is not None
            or rpc_url.query
            or rpc_url.fragment
        ):
            raise ValueError("XRPL_RPC_URL must be an absolute URL without credentials")
        if rpc_url.scheme != "https":
            is_loopback = hostname.rstrip(".").lower() == "localhost"
            try:
                is_loopback = is_loopback or ipaddress.ip_address(
                    hostname.rstrip(".")
                ).is_loopback
            except ValueError:
                pass
            if not (
                rpc_url.scheme == "http"
                and self.ALLOW_INSECURE_XRPL_RPC
                and is_loopback
            ):
                raise ValueError(
                    "XRPL_RPC_URL must use HTTPS; loopback HTTP requires ALLOW_INSECURE_XRPL_RPC"
                )
        if self.GATEWAY_AUTH_MODE == "single_token":
            if self.FACILITATOR_BEARER_TOKEN is None:
                raise ValueError(
                    "FACILITATOR_BEARER_TOKEN is required when GATEWAY_AUTH_MODE=single_token"
                )
        if self.MPP_DEFAULT_REALM is not None and not self.MPP_DEFAULT_REALM.strip():
            raise ValueError("MPP_DEFAULT_REALM must be non-empty when provided")
        if (
            self.PAYCHANNEL_IDLE_CLOSE_SECONDS > 0
            and self.PAYCHANNEL_REDEEM_INTERVAL_SECONDS == 0
        ):
            raise ValueError(
                "PAYCHANNEL_IDLE_CLOSE_SECONDS requires "
                "PAYCHANNEL_REDEEM_INTERVAL_SECONDS"
            )
        if self.PAYCHANNEL_REDEEM_BATCH_SIZE > 1000:
            raise ValueError("PAYCHANNEL_REDEEM_BATCH_SIZE must not exceed 1000")
        return self

    def challenge_secrets(self) -> tuple[str, ...]:
        """Active HMAC key followed by verification-only rotation keys."""

        active = self.MPP_CHALLENGE_SECRET.get_secret_value().strip()
        previous = tuple(
            item.strip()
            for item in self.MPP_CHALLENGE_PREVIOUS_SECRETS.split(",")
            if item.strip()
        )
        return (active, *previous)


@lru_cache
def get_settings() -> Settings:
    return Settings()
