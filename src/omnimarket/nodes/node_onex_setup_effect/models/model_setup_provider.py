# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic model for a single model provider configuration."""

from __future__ import annotations

import re
import urllib.parse
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelProvider,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)

_URL_RE = re.compile(r"^[^\s\x00-\x1f\x7f-\x9f]*$")


class ModelSetupProvider(BaseModel):
    """A single model provider configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    models: tuple[str, ...]
    url: str | None = None
    api_key_required: bool = False
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    native_messages: bool | None = None
    make_default: bool = False

    @field_validator("models")
    @classmethod
    def _validate_models(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("models must be non-empty")
        cleaned = tuple(m.strip() for m in v)
        if any(not m for m in cleaned):
            raise ValueError("models must be non-empty strings")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("models must be unique")
        return cleaned

    @field_validator("url")
    @classmethod
    def validate_endpoint(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _URL_RE.match(v):
            raise ValueError("url must not contain whitespace or control characters")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        parts = urllib.parse.urlsplit(v)
        if parts.username is not None or parts.password is not None:
            raise ValueError("url must not contain userinfo")
        if parts.query:
            raise ValueError("url must not contain a query string")
        if parts.fragment:
            raise ValueError("url must not contain a fragment")
        if not parts.hostname:
            raise ValueError("url must have a non-empty hostname")
        if parts.port is not None and not (0 < parts.port <= 65535):
            raise ValueError("url must have a valid port")
        return v

    def model_post_init(self, __context: object) -> None:
        if self.connection_id is not None and self.url is None:
            raise ValueError("Named connections require an explicit endpoint URL")
        if self.provider in setup_provider_traits().url_required and self.url is None:
            raise ValueError(f"provider {self.provider} requires url")

    @property
    def credential_identity(self) -> str:
        return (
            "connection:" + self.connection_id if self.connection_id else self.provider
        )

    @property
    def registration_provider(self) -> ModelProvider:
        """Canonical credential/endpoint owner for this logical selection."""
        return cast(
            "ModelProvider",
            setup_provider_traits().registration(
                self.provider, keyed=bool(self.api_key_required)
            ),
        )

    @property
    def needs_key(self) -> bool:
        return self.registration_provider != "local"
