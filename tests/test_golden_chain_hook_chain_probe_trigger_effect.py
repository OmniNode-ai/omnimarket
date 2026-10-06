# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_hook_chain_probe_trigger_effect."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_hook_chain_probe_trigger_effect.handlers.handler_hook_chain_probe_trigger import (
    HandlerHookChainProbeTrigger,
)
from omnimarket.nodes.node_hook_chain_probe_trigger_effect.models.model_hook_chain_probe_trigger import (
    ModelHookChainProbeHeartbeat,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_hook_chain_probe_trigger_effect/contract.yaml"
)


def test_heartbeat_in_command_out_then_throttled() -> None:
    now = [10_000.0]
    handler = HandlerHookChainProbeTrigger(clock=lambda: now[0], interval_seconds=900)
    assert handler.handle(ModelHookChainProbeHeartbeat()) is not None
    assert handler.handle(ModelHookChainProbeHeartbeat()) is None
    now[0] += 900
    assert handler.handle(ModelHookChainProbeHeartbeat()) is not None


def test_terminal_event_is_the_probe_command_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert (
        contract["terminal_event"]
        == "onex.cmd.omnimarket.hook-chain-probe-requested.v1"
    )
    assert contract["probe_schedule"]["probe_interval_seconds"] == 900
