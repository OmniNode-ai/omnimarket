# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent trigger protocol of node_branch_deploy_command_effect."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelTriggerOutcome(BaseModel):
    """The outcome of running the deploy agent trigger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str
    stderr: str


class ProtocolDeployAgentTrigger(Protocol):
    """The deploy agent trigger port the handler takes as a constructor argument."""

    def run(
        self,
        argv: Sequence[str],
        env_file: str,
        timeout_seconds: int,
    ) -> ModelTriggerOutcome: ...
