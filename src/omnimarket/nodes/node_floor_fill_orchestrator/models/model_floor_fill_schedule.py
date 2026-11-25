# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick unit's contract block and the result of a schedule verb."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class ModelFloorFillUnitEnvironment(BaseModel):
    """The names of the three variables the live unit carries (the contract's ``schedule.unit_environment``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append_command: str = Field(min_length=1)
    workspace_config_root: str = Field(min_length=1)
    overlay_roots: str = Field(min_length=1)

    def names(self) -> tuple[str, ...]:
        return (
            self.ledger_append_command,
            self.workspace_config_root,
            self.overlay_roots,
        )


class ModelFloorFillSchedule(BaseModel):
    """The contract's ``schedule`` block: the unit's name, its deadline, the dispatch venv it runs in and the live
    unit's environment (which host row cells build it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    dispatch_venv: str = Field(min_length=1)
    ledger_append_entry: str = Field(min_length=1)
    unit_environment: ModelFloorFillUnitEnvironment


class ModelFloorFillScheduleResult(BaseModel):
    """What a schedule verb prints and exits with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int = Field(ge=0)
    stdout: str = ""
    stderr: str = ""


def load_floor_fill_schedule(path: Path = CONTRACT_PATH) -> ModelFloorFillSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelFloorFillSchedule.model_validate(contract["schedule"])


__all__: list[str] = [
    "CONTRACT_PATH",
    "ModelFloorFillSchedule",
    "ModelFloorFillScheduleResult",
    "ModelFloorFillUnitEnvironment",
    "load_floor_fill_schedule",
]
