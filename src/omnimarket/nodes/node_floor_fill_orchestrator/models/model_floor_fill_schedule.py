# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick unit's contract block and the result of a schedule verb."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class ModelFloorFillSchedule(BaseModel):
    """The contract's ``schedule`` block: the unit's name, its deadline and the dispatch venv it runs in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    dispatch_venv: str = Field(min_length=1)


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
    "load_floor_fill_schedule",
]
