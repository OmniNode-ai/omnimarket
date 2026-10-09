# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parts of a committed summary.json that replay reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceReplayAgreement(BaseModel):
    """One judge's agreement with the calibration labels."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    n: int = Field(strict=True, gt=0)
    agree: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    kappa: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False)


class ModelAcceptanceReplayJudgeCalibration(BaseModel):
    """The calibration block of one judge; ``all`` is the matrix trials and hand labels together."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    overall: ModelAcceptanceReplayAgreement = Field(alias="all")


class ModelAcceptanceReplayCalibration(BaseModel):
    """The calibration section of summary.json: one block per judge.

    The committed section also holds blocks that are not a judge's (the two judges' agreement
    with each other, the quality gate's), with other shapes; they are ignored.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    codex: ModelAcceptanceReplayJudgeCalibration | None = None
    opus: ModelAcceptanceReplayJudgeCalibration | None = None


class ModelAcceptanceReplayCell(BaseModel):
    """One matrix cell; ``n`` is the count of judged items it holds."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    n: int = Field(strict=True, ge=0)


class ModelAcceptanceReplaySummary(BaseModel):
    """What replay reads of summary.json: who judged, how well, and how many items."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    primary_judge: Literal["codex", "opus"]
    calibration: ModelAcceptanceReplayCalibration
    matrix: tuple[ModelAcceptanceReplayCell, ...]
