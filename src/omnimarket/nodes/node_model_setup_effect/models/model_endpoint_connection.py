# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named server configuration; credentials are excluded from serializable requests."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from omnimarket.models.model_endpoint_models import ModelEndpointModelsRequest

from .model_model_setup_result import ModelModelTestResult


class ModelEndpointConnection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    provider: str
    url: str
    models: tuple[str, ...]
    secret_ref: str | None = None
    native_messages: bool = False

    @property
    def backend_ids(self) -> tuple[str, ...]:
        return tuple(self.backend_id(model) for model in self.models)

    def backend_id(self, model: str, *, native: bool = False) -> str:
        digest = hashlib.sha256(model.encode()).hexdigest()[:16]
        return f"connection-{self.connection_id}-{'native-' if native else ''}{digest}"


class ModelEndpointConnectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["connect", "list", "refresh", "select", "remove", "test"]
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    url: str | None = None
    models: tuple[str, ...] = ()
    key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    prove: bool = True
    native_messages: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(c.isspace() for c in value):
            raise ValueError("The endpoint URL must not contain whitespace")
        return ModelEndpointModelsRequest(url=value).url

    @field_validator("models")
    @classmethod
    def ordered_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() or name != name.strip() for name in value) or len(
            set(value)
        ) != len(value):
            raise ValueError("Choose unique nonempty model IDs in preference order")
        return value


class ModelEndpointConnectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready", "refused", "failed"]
    connections: tuple[ModelEndpointConnection, ...] = ()
    catalog: tuple[str, ...] = ()
    tests: tuple[ModelModelTestResult, ...] = ()
    message: str | None = None
