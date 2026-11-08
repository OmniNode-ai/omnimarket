# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay that selects the bus and the observation settings of a scenario run."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ENV = "ONEX_CHAIN_SCENARIO_OVERLAY"
_PACKAGED_DEFAULT = (
    Path(__file__).parent / "overlays" / "chain_scenario.inmemory.overlay.yaml"
)


class ModelChainScenarioOverlay(BaseModel):
    """Bus kind, broker bootstrap variable, deadline and failure-row type for a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bus: Literal["inmemory", "kafka"]
    kafka_bootstrap_env: str | None = Field(default=None, min_length=1)
    terminal_deadline_ms: int = Field(..., gt=0)
    failure_row_type: str = Field(..., min_length=1)
    backend_group: str | None = Field(
        default=None,
        min_length=1,
        description="Entry-point group that provides the broker bus.",
    )
    backend_name: str | None = Field(
        default=None,
        min_length=1,
        description="Entry point in that group that is the broker bus.",
    )
    scrub_env: tuple[str, ...] = Field(
        default=(),
        description="Environment variables removed for the run, so an in-memory run never sees a real bus.",
    )

    def backend_overrides(self) -> dict[str, str]:
        """The bus settings this overlay stands for, with the broker read from the environment."""
        if self.bus == "inmemory":
            return {"event_bus": "inmemory"}
        if (
            self.kafka_bootstrap_env is None
            or self.backend_group is None
            or self.backend_name is None
        ):
            raise ModelOnexError(
                message="a kafka chain scenario overlay names the bootstrap variable and the bus entry point",
                error_code=EnumCoreErrorCode.CONFIGURATION_ERROR,
            )
        bootstrap = os.environ.get(self.kafka_bootstrap_env, "").strip()
        if not bootstrap:
            raise ModelOnexError(
                message=f"{self.kafka_bootstrap_env} is not set; the broker overlay has no instance to run against",
                error_code=EnumCoreErrorCode.CONFIGURATION_ERROR,
            )
        return {"event_bus": "kafka", "kafka_bootstrap": bootstrap}


def load_overlay(path: Path | None = None) -> ModelChainScenarioOverlay:
    """Load the overlay named by ``path``, else by the environment, else the packaged in-memory one."""
    chosen = path
    if chosen is None:
        named = os.environ.get(OVERLAY_ENV, "").strip()
        chosen = Path(named) if named else _PACKAGED_DEFAULT
    if not chosen.is_file():
        raise ModelOnexError(
            message=f"chain scenario overlay not found: {chosen}",
            error_code=EnumCoreErrorCode.CONFIGURATION_ERROR,
        )
    return ModelChainScenarioOverlay.model_validate(
        yaml.safe_load(chosen.read_text(encoding="utf-8"))
    )


__all__ = ["OVERLAY_ENV", "ModelChainScenarioOverlay", "load_overlay"]
