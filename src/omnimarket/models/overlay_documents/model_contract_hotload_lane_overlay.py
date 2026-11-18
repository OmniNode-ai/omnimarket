# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/runtime_contract_hotload/overlay.yaml through (OMN-24215).

omnibase_infra reads the same document through its own ModelContractHotloadLaneOverlay; no value
lives here. The document accepts and refuses exactly what that model does, held by the seam corpus in
its test (an empty lanes map and a blank lane name are accepted by both).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ModelContractHotloadLaneRow(BaseModel):
    """One lane entry inside the contract hotload overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    mode: Literal["on_demand", "full_load"] | None = None
    allowed_environments: tuple[str, ...] = ()

    @field_validator("allowed_environments", mode="after")
    @classmethod
    def _validate_allowed_environments(
        cls, environments: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Store each environment name stripped and lowercased; refuse a blank name."""
        normalized: list[str] = []
        for environment in environments:
            name = environment.strip().lower()
            if not name:
                raise ValueError("environment names must not be blank")
            normalized.append(name)
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_enabled_lane(self) -> Self:
        """An enabled lane must declare a mode and at least one allowed environment."""
        if self.enabled and self.mode is None:
            raise ValueError("an enabled lane must set mode")
        if self.enabled and not self.allowed_environments:
            raise ValueError("an enabled lane must list allowed_environments")
        return self


class ModelContractHotloadLaneOverlay(BaseModel):
    """The contract hotload lane overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    lanes: dict[str, ModelContractHotloadLaneRow]


__all__ = [
    "ModelContractHotloadLaneOverlay",
    "ModelContractHotloadLaneRow",
]
