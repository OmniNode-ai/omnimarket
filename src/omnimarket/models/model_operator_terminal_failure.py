# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A handler failure the runtime routes to its contract-declared failure terminal."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelOperatorTerminalFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["failed"] = "failed"
    terminal_failure_cause: str
