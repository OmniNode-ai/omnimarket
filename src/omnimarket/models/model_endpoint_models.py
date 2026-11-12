# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Connection-scoped model listing; discovery is not an inference proof."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from omnimarket.handlers.routing import byok_provider_backends
from omnimarket.handlers.routing.byok_model_discovery import DISCOVERY_TIMEOUT_SECONDS


class ModelEndpointModelsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    provider: str | None = None
    key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: float = Field(default=DISCOVERY_TIMEOUT_SECONDS, gt=0, le=60)

    @property
    def backend(self) -> byok_provider_backends.ModelByokProviderBackend | None:
        return (
            byok_provider_backends.load_byok_provider_catalog().get(self.provider)
            if self.provider
            else None
        )

    @model_validator(mode="after")
    def declared_hosted_provider(self) -> "ModelEndpointModelsRequest":
        if self.provider is not None:
            backend = self.backend
            if (
                backend is None
                or not backend.customer_routable
                or backend.endpoint_from_registration
            ):
                raise ValueError(
                    "Choose a declared hosted provider or use an explicit endpoint"
                )
            if self.key is None or not self.key.get_secret_value():
                raise ValueError("A hosted provider requires its own API key")
        return self

    @field_validator("url")
    @classmethod
    def endpoint_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
        ):
            raise ValueError(
                "Use an HTTP(S) base URL without credentials, query or fragment"
            )
        # Access validates malformed/out-of-range ports before any network call.
        _ = parsed.port
        return value.rstrip("/").removesuffix("/v1")


class ModelEndpointModelsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    status: Literal["ready", "rejected", "unsupported", "unavailable", "invalid"]
    models: tuple[str, ...] = ()
    message: str | None = None
