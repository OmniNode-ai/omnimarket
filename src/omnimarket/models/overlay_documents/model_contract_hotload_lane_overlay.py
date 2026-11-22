# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/runtime_contract_hotload/overlay.yaml through (OMN-24215).

omnibase_infra reads the same document through its own ModelContractHotloadLaneOverlay; no value
lives here. The document accepts and refuses exactly what that model does, held by the seam corpus in
its test (an empty lanes map and a blank lane name are accepted by both).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelContractHotloadLazyStart(BaseModel):
    """The optional boot set of a lane row: the front door, the contracts kept eager, a start and a retry budget.

    Accepts and refuses exactly what omnibase_infra's ModelLazyStartPolicy does: names are not blank, the
    eager names do not repeat, the budget is over zero and the retries are zero or more. No name lives here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingress_contract: str = Field(min_length=1)
    boot_contracts: tuple[str, ...] = ()
    budget_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)

    @model_validator(mode="after")
    def _names_are_unique_and_not_blank(self) -> Self:
        """Refuse a blank name anywhere in the set and a repeated eager name."""
        names = (self.ingress_contract, *self.boot_contracts)
        if any(not name.strip() for name in names):
            raise ValueError("lazy_start names must not be blank")
        if len(set(self.boot_contracts)) != len(self.boot_contracts):
            raise ValueError("lazy_start.boot_contracts must not repeat a name")
        return self


class ModelContractHotloadLaneRow(BaseModel):
    """One lane entry inside the contract hotload overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    mode: Literal["on_demand", "full_load"] | None = None
    allowed_environments: tuple[str, ...] = ()
    lazy_start: ModelContractHotloadLazyStart | None = None

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
    "ModelContractHotloadLazyStart",
]
