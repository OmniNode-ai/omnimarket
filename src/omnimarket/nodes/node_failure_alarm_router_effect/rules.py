# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rules the failure alarm router reads from its contract: cadence, latency budget and streak thresholds (OMN-26216).

Change a value in ``config.failure_alarm_router`` of the contract and the alarms change with no code change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
)

CONTRACT = Path(__file__).with_name("contract.yaml")


class ModelSeamDeadlineRules(BaseModel):
    """Seam deadline probe rows (``src=seam-deadline``) the router reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_cell: str = Field(min_length=1)
    latency_budget_ms: int = Field(gt=0)


class ModelDelegationStreakRules(BaseModel):
    """Delegation STATUS rows (``src=delegation``): failed rows of one model and one failure class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_cell: str = Field(min_length=1)
    min_failed_rows: int = Field(ge=2)
    quiet_minutes: int = Field(gt=0)
    unnamed_models: tuple[str, ...]


class ModelFailureAlarmRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: ModelRuntimeTickScheduleConfig
    seam_deadline: ModelSeamDeadlineRules
    delegation_streak: ModelDelegationStreakRules


@lru_cache(maxsize=1)
def load_rules(contract: Path = CONTRACT) -> ModelFailureAlarmRules:
    """The ``config.failure_alarm_router`` block of the contract; a missing or unknown rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    return ModelFailureAlarmRules.model_validate(data["config"]["failure_alarm_router"])


__all__ = [
    "CONTRACT",
    "ModelDelegationStreakRules",
    "ModelFailureAlarmRules",
    "ModelSeamDeadlineRules",
    "load_rules",
]
