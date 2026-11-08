# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed tool and calibration policies, loaded from contract data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelStubPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    refused_programs: tuple[str, ...]
    network_programs: tuple[str, ...]
    network_git_verbs: tuple[str, ...]
    ledger_programs: tuple[str, ...]
    network_tools: tuple[str, ...]
    class_depth: dict[str, int]


class ModelStabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    jaccard_tolerance: float = Field(strict=True, ge=0, le=1)
    min_repeats: Literal[3]


class ModelContinuationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repeats: Literal[3]
    default_turns: int = Field(gt=0)
    sensitivity_turns: tuple[int, ...]
    temperature: float = Field(ge=0)
    stability_data_file: str = Field(min_length=1)
    container_factory: str = Field(min_length=1)


class ModelHandlerBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    module: str
    name: str


class ModelHandlerComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handler: ModelHandlerBinding
