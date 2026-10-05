# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_hook_chain_probe_verdict_effect."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_hook_chain_probe_verdict_effect.handlers.handler_hook_chain_probe_verdict import (
    HandlerHookChainProbeVerdict,
)
from omnimarket.nodes.node_hook_chain_probe_verdict_effect.models.model_hook_chain_probe_verdict import (
    ModelHookChainHealthRecorded,
    ModelHookChainProbeOutcome,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_hook_chain_probe_verdict_effect/contract.yaml"
)


def test_broken_chain_is_recorded_unhealthy() -> None:
    recorded = HandlerHookChainProbeVerdict().handle(
        ModelHookChainProbeOutcome(
            correlation_id="c-1",
            chain_complete=False,
            failed_leg="forwarder_relay",
            primary_blocker="allowlist_denied",
        )
    )
    assert isinstance(recorded, ModelHookChainHealthRecorded)
    assert recorded.healthy is False
    assert recorded.primary_blocker == "allowlist_denied"


def test_terminal_event_and_both_probe_terminals_are_declared() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.hook-chain-health-recorded.v1"
    )
    assert set(contract["event_bus"]["subscribe_topics"]) == {
        "onex.evt.omnimarket.hook-chain-probe-completed.v1",
        "onex.evt.omnimarket.hook-chain-probe-failed.v1",
    }
