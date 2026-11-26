# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the A/B compare orchestrator's model registry through.

The document is the one ``node_ab_compare_orchestrator`` reads as its registry: ``models``, a list of entries, each naming
where one OpenAI-compatible model answers (an ``endpoint`` base URL, or ``endpoint_env`` naming the variable that holds it),
the served model id, its location, context window and its price per 1k tokens. An entry holds no secret: ``requires_key``
names an environment variable, never a value. No other key is accepted and an id is declared once.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

NonEmpty = Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
Price = Annotated[StrictInt | StrictFloat, Field(ge=0)]


class ModelAbCompareModelEntry(BaseModel):
    """One registry entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmpty
    display_name: NonEmpty
    protocol: Literal["openai_compatible"]
    cost_per_1k_input: Price
    cost_per_1k_output: Price
    location: Literal["local", "cloud"]
    context_window: Annotated[StrictInt, Field(ge=1)]
    endpoint: NonEmpty | None = None
    endpoint_env: NonEmpty | None = None
    model_id: NonEmpty | None = None
    model_id_env: NonEmpty | None = None
    model_id_default: StrictStr | None = None
    requires_key: NonEmpty | None = None

    @field_validator("endpoint")
    @classmethod
    def _http_base(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("endpoint is an http or https base URL with a host")
        return value

    @model_validator(mode="after")
    def _names_an_address(self) -> Self:
        if self.endpoint is None and self.endpoint_env is None:
            raise ValueError("an entry names an endpoint or an endpoint_env")
        return self


class ModelAbCompareModelsOverlay(BaseModel):
    """The ``node_ab_compare_orchestrator`` overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: StrictStr
    models: Annotated[list[ModelAbCompareModelEntry], Field(min_length=1)]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        ids = [m.id for m in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("a model id is declared once")
        return self


__all__ = ["ModelAbCompareModelEntry", "ModelAbCompareModelsOverlay"]
