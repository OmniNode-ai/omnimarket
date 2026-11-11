# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Connection-scoped model listing; discovery is not an inference proof."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from omnimarket.handlers.routing.byok_model_discovery import DISCOVERY_TIMEOUT_SECONDS


class ModelEndpointModelsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: float = Field(default=DISCOVERY_TIMEOUT_SECONDS, gt=0, le=60)

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
