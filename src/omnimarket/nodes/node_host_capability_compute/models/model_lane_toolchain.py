# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed readings and verdicts of the lane toolchain parity read.

A reading is what a host's probe of a declared tool measured: the exit status (``None`` when not measured, 127 when the command
was not found) and the tail of its output. Nothing here holds a credential.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_toolchain import ModelLaneToolchainTable


class ModelToolReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    status: int | None = None
    tail: str = ""


class ModelHostToolchainReading(BaseModel):
    """One host's measured probes. ``empty_dirs`` lists the declared repair directories the host holds empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    tools: tuple[ModelToolReading, ...]
    empty_dirs: tuple[str, ...] = ()


class ModelLaneToolchainRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: ModelLaneToolchainTable
    readings: tuple[ModelHostToolchainReading, ...]


class ModelLaneToolchainFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    tool: str
    state: Literal["missing", "broken", "undeclared"]
    reason: str


class ModelLaneToolchainResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Reading order, then the host's declared tool order; empty when every measured tool works.
    failures: tuple[ModelLaneToolchainFailure, ...]
