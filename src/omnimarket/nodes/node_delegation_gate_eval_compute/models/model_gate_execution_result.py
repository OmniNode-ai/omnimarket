# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Previously recorded execution evidence; this node never executes code."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelGateExecutionResult(BaseModel):
    """Previously recorded execution evidence; this node never executes code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    detail: str = ""
