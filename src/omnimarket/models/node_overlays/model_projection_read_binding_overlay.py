# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``projection_read_binding`` overlay: per-host projection read binding (OMN-23994).

The projection read node reads the overlay file
``<overlay root>/node_projection_read_effect/overlay.yaml`` and picks the row whose
``host`` matches the local host name. This model is what the overlay home validates
the document through, so it imports pydantic alone: that check installs pydantic
and pyyaml and nothing else. It checks the shape of the document and of every
value; which hosts may appear is the operator's choice, so no host name and no
database value of any host lives here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SCHEMA_VERSION = "projection_read_binding.v1"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _secret_ref_prefix(value: str) -> str:
    if value.startswith("env:") or value.startswith("settings:"):
        return value
    raise ValueError(
        "must start with 'env:' or 'settings:'; an inline database URL is refused "
        "so the model never holds a credential"
    )


SecretRef = Annotated[str, AfterValidator(_secret_ref_prefix)]


class ModelProjectionReadHostBinding(BaseModel):
    """The projection read binding one host's read node uses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: NonBlank
    kafka_bootstrap_servers: NonBlank
    kafka_consumer_group: NonBlank
    database_url_secret_ref: SecretRef


class ModelProjectionReadBindingOverlay(BaseModel):
    """``projection_read_binding``: the read binding of each host, by host name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["projection_read_binding.v1"]
    bindings: tuple[ModelProjectionReadHostBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_host_names(self) -> ModelProjectionReadBindingOverlay:
        seen: set[str] = set()
        for row in self.bindings:
            key = row.host.casefold()
            if key in seen:
                raise ValueError(
                    f"two rows name the same host {row.host!r} ignoring case"
                )
            seen.add(key)
        return self


__all__ = [
    "SCHEMA_VERSION",
    "ModelProjectionReadBindingOverlay",
    "ModelProjectionReadHostBinding",
]
