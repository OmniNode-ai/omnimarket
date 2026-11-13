# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The argv flag names the handler emits must be a subset of the deploy agent's trigger flags (OMN-24027)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_in_memory_trigger import (
    InMemoryDeployAgentTrigger,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_deploy_agent_trigger import (
    ModelTriggerOutcome,
)
from tests.nodes.node_branch_deploy_command_effect.support import overlay, request

pytestmark = pytest.mark.unit

FLAG_FILE = Path(__file__).resolve().parent / "trigger_flags.json"


def _flags() -> set[str]:
    return set(json.loads(FLAG_FILE.read_text()))


def _argv_for(mode: str) -> list[str]:
    trigger = InMemoryDeployAgentTrigger(
        outcomes=[ModelTriggerOutcome(exit_code=0, stdout="Published.", stderr="")]
    )
    handler = HandlerBranchDeployCommand(overlay=overlay(), trigger=trigger)
    handler.handle(request(mode=mode))
    return list(trigger.calls[0][0])


def test_deploy_argv_flags_are_in_the_golden_list() -> None:
    flags = _flags()
    argv = _argv_for("deploy")
    emitted = {a for a in argv if a.startswith("--")}
    assert emitted <= flags


def test_rollback_argv_flags_are_in_the_golden_list() -> None:
    flags = _flags()
    argv = _argv_for("rollback")
    emitted = {a for a in argv if a.startswith("--")}
    assert emitted <= flags


def test_control_unknown_flag_is_detected() -> None:
    flags = _flags()
    emitted = {"--no-such-flag"}
    assert not (emitted <= flags)
