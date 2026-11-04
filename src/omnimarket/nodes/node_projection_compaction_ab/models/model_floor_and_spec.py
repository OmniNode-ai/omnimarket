# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed readers for the live-measurement data file and the floor file."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

_DATA = Path(__file__).parent.parent / "data"


class EnumDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class ModelWriterSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: tuple[str, ...]


class ModelLiveMeasure(BaseModel):
    """A live hypothesis or safeguard: numerator column over an observable denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str | None = None
    favourable_on_mod: str | None = None
    numerator_column: str
    denominator: str


class ModelLiveMeasurementSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_columns: tuple[str, ...]
    writers: dict[str, ModelWriterSpec]
    live_hypotheses: tuple[ModelLiveMeasure, ...]
    live_safeguards: tuple[ModelLiveMeasure, ...]
    replay_only: tuple[str, ...]


class ModelCriterionFloor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: EnumDirection
    floor: float | None


class ModelFloorFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    helps_verdict_seen: bool
    criteria: dict[str, ModelCriterionFloor]


def load_live_spec(path: Path | None = None) -> ModelLiveMeasurementSpec:
    with (path or _DATA / "live_measurement.yaml").open(encoding="utf-8") as handle:
        return ModelLiveMeasurementSpec.model_validate(yaml.safe_load(handle))


def load_floor_file(path: Path | None = None) -> ModelFloorFile:
    with (path or _DATA / "floor.yaml").open(encoding="utf-8") as handle:
        return ModelFloorFile.model_validate(yaml.safe_load(handle))


__all__ = [
    "EnumDirection",
    "ModelCriterionFloor",
    "ModelFloorFile",
    "ModelLiveMeasure",
    "ModelLiveMeasurementSpec",
    "ModelWriterSpec",
    "load_floor_file",
    "load_live_spec",
]
