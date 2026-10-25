# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the clone parity node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCloneParityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str


class ModelCloneParityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clone_parity_py: str
    failure_item: str
    failure_reason: str
