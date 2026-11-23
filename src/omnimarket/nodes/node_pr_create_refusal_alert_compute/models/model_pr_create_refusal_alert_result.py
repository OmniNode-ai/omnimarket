# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refusals of ``onex scm pr-create`` by cause, and the one alert each new cause raises."""

from pydantic import BaseModel, ConfigDict, Field


class ModelPrCreateRefusalClass(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: str
    count: int = Field(ge=1, description="STATUS and TERMINAL rows in this class.")
    lanes: tuple[str, ...] = Field(
        description="The lanes whose rows are in this class, first hit first."
    )
    first_lane: str
    first_stamp: str


class ModelPrCreateRefusalAlert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: str
    first_lane: str
    first_stamp: str
    lanes_so_far: int = Field(ge=1)
    message: str


class ModelPrCreateRefusalAlertResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: str
    window_end: str
    rows_read: int = Field(ge=0, description="Rows given.")
    rows_considered: int = Field(
        ge=0, description="STATUS and TERMINAL rows, the only kinds read."
    )
    classes: tuple[ModelPrCreateRefusalClass, ...] = Field(
        description="Causes with at least one row, largest first."
    )
    alerts: tuple[ModelPrCreateRefusalAlert, ...] = Field(
        description="One alert for each cause with a row that was not already alerted, naming the first lane that hit it."
    )
    suppressed: tuple[str, ...] = Field(
        description="Causes with rows that raised no alert because one was already raised."
    )
