# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review replay set's freeze command has a declared trigger (OMN-26438).

The freeze is on demand by design: the set is frozen once per rollout, before the first roster is
scored, and every later replay is measured against it. A schedule would freeze a new set each time
the landing branches move and so replace the set a baseline was measured on, and the freeze needs
the canonical clones of the host that runs it, which the shared runtime does not hold. The trigger
is therefore the operator's ``onex run-node`` command, which publishes the freeze command.

``declared_triggers`` reads a contract the way the rule states it: a schedule on the runtime tick,
another node contract that publishes the command topic, or an external trigger that names the
command that publishes it. A contract that declares none is a node nothing runs, and the control
tests show the check names no trigger for it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE = "node_review_replay_set_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / NODE / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _producers_of(topic: str) -> list[str]:
    """Other node contracts that declare ``topic`` among their publish topics."""
    found: list[str] = []
    for path in sorted(NODES.glob("node_*/contract.yaml")):
        if path.parent.name == NODE:
            continue
        loaded = yaml.safe_load(path.read_text("utf-8"))
        bus = loaded.get("event_bus") if isinstance(loaded, dict) else None
        if isinstance(bus, dict) and topic in (bus.get("publish_topics") or []):
            found.append(path.parent.name)
    return found


def declared_triggers(contract: dict[str, Any], *, producers: list[str]) -> list[str]:
    """The triggers a contract declares; empty when nothing would ever run the node."""
    triggers: list[str] = []
    subscribed = {s["topic"] for s in contract.get("input_subscriptions") or []}
    for key, block in (contract.get("config") or {}).items():
        if isinstance(block, dict) and "schedule" in block and TICK_TOPIC in subscribed:
            triggers.append(f"schedule:{key}")
    triggers.extend(f"producer:{name}" for name in producers)
    dispatch = contract.get("runtime_dispatch") or {}
    external = dispatch.get("external_trigger")
    if (
        isinstance(external, str)
        and f"onex run-node {contract['name']}" in external
        and dispatch.get("command_topic") in (contract["event_bus"]["subscribe_topics"])
    ):
        triggers.append("external:onex run-node")
    return triggers


def test_the_freeze_command_declares_a_trigger() -> None:
    contract = _contract()
    producers = _producers_of(contract["runtime_dispatch"]["command_topic"])
    assert declared_triggers(contract, producers=producers), (
        f"{NODE} declares no schedule, no producer node and no external trigger"
    )


def test_the_trigger_is_the_operator_command_that_publishes_the_freeze() -> None:
    contract = _contract()
    assert declared_triggers(contract, producers=[]) == ["external:onex run-node"]
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.review-replay-set-freeze-requested.v1"
    )
    assert "review_replay_set.freeze" in {
        h["operation"] for h in contract["handler_routing"]["handlers"]
    }
    # On demand by design: no schedule, so no tick subscription and no config.schedule block.
    assert "config" not in contract
    assert TICK_TOPIC not in contract["event_bus"]["subscribe_topics"]


def test_control_a_contract_with_no_trigger_declares_none() -> None:
    bare = copy.deepcopy(_contract())
    del bare["runtime_dispatch"]["external_trigger"]
    assert declared_triggers(bare, producers=[]) == []


def test_control_an_external_trigger_naming_another_command_declares_none() -> None:
    other = copy.deepcopy(_contract())
    other["runtime_dispatch"]["external_trigger"] = "run it by hand when needed"
    assert declared_triggers(other, producers=[]) == []


def test_control_a_producer_node_alone_declares_a_trigger() -> None:
    bare = copy.deepcopy(_contract())
    del bare["runtime_dispatch"]["external_trigger"]
    assert declared_triggers(bare, producers=["node_x"]) == ["producer:node_x"]
