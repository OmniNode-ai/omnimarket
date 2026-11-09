# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the example-envelope routes: request, contract settings, overlay, route."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelEnvelopeExampleRequest(BaseModel):
    """The route takes no input: any body is ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelEnvelopeExampleSettings(BaseModel):
    """Every value of the example, read from the contract ``config`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node: str = Field(min_length=1)
    envelope_version: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    tags: dict[str, Any]
    payload: dict[str, str]
    generated_id_fields: tuple[str, ...] = ()

    @field_validator("payload")
    @classmethod
    def _payload_values_not_blank(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(not item.strip() for item in value.values()):
            raise ValueError("payload values must be non-empty strings")
        return value


class ModelEnvelopeExampleOverlay(BaseModel):
    """A deployment overlay: only the values it names replace the contract's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node: str | None = Field(default=None, min_length=1)
    envelope_version: str | None = Field(default=None, min_length=1)
    operation: str | None = Field(default=None, min_length=1)
    tags: dict[str, Any] | None = None
    payload: dict[str, str] | None = None

    @model_validator(mode="after")
    def _named_means_present(self) -> ModelEnvelopeExampleOverlay:
        # A key the overlay names must carry a value; an absent key is not named.
        empty = sorted(n for n in self.model_fields_set if getattr(self, n) is None)
        if empty:
            raise ValueError(f"overlay names no value for {empty}")
        return self

    def named_values(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ModelEnvelopeExampleHandlerRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    module: str


class ModelEnvelopeExampleRoute(BaseModel):
    """The ``http_ingress`` contract block: the route this node's app serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handler: ModelEnvelopeExampleHandlerRef
    path: str = Field(pattern=r"^/[A-Za-z0-9._~\-/]+$")
    methods: tuple[Literal["GET", "POST"], ...] = Field(min_length=1)
    authorization: Literal["anonymous"]
    input_schema: dict[str, Any]
    response_status: int = Field(ge=200, lt=300)


__all__ = [
    "ModelEnvelopeExampleHandlerRef",
    "ModelEnvelopeExampleOverlay",
    "ModelEnvelopeExampleRequest",
    "ModelEnvelopeExampleRoute",
    "ModelEnvelopeExampleSettings",
]
