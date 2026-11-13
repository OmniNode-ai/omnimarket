# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory adapter for the deploy agent trigger."""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_deploy_agent_trigger import (
    ModelTriggerOutcome,
)


class InMemoryDeployAgentTrigger:
    """Records every call and answers the queued outcomes."""

    def __init__(self, outcomes: list[ModelTriggerOutcome] | None = None) -> None:
        self.calls: list[tuple[Sequence[str], str, int]] = []
        self._outcomes: list[ModelTriggerOutcome] = list(outcomes or [])

    def run(
        self,
        argv: Sequence[str],
        env_file: str,
        timeout_seconds: int,
    ) -> ModelTriggerOutcome:
        self.calls.append((argv, env_file, timeout_seconds))
        if self._outcomes:
            return self._outcomes.pop(0)
        return ModelTriggerOutcome(exit_code=0, stdout="Published.", stderr="")
