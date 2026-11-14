# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent key check reading of node_branch_deploy_command_effect."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelAgentKeyCheckReading(BaseModel):
    """The reading of one agent key check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["read", "unreachable", "absent"]
    key_check: str = ""
    detail: str = ""
