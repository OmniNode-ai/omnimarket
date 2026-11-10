# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay consumer join check ends in the contract's terminal event (OMN-23150).

The contract-state-coverage gate reads only the repository ``tests/`` tree, and this node's chain
tests live beside the node. This file runs the node's golden chain and its error chain through the
in-memory bus and asserts the declared terminal event on the last event of each, so the declared
state is asserted where the gate looks; the second test shows the assertion turns red on a named
break (a renamed terminal topic).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_overlay_drift_compute.tests.join_support import NODE, World
from omnimarket.nodes.node_overlay_drift_compute.tests.test_golden_chain_overlay_consumer_join import (
    PAYLOAD,
    publish,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

TERMINAL_EVENT = "onex.evt.omnimarket.overlay-consumer-join-run-completed.v1"


def _declared_terminal_event() -> str:
    contract = yaml.safe_load(Path(NODE / "contract.yaml").read_text(encoding="utf-8"))
    return str(contract["terminal_event"])


def test_golden_chain_last_event_is_the_declared_terminal_event() -> None:
    events = asyncio.run(publish(World(), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == _declared_terminal_event()
    assert events[-1][0] == TERMINAL_EVENT


def test_error_chain_last_event_is_the_declared_terminal_event() -> None:
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    events = asyncio.run(publish(world, InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == _declared_terminal_event()
    assert events[-1][0] == TERMINAL_EVENT


def test_named_break_a_renamed_terminal_topic_turns_the_assertion_red() -> None:
    events = asyncio.run(publish(World(), InMemoryLedger(), PAYLOAD))
    renamed = TERMINAL_EVENT.replace("run-completed", "run-finished")
    assert events[-1][0] != renamed
    with pytest.raises(AssertionError):
        assert events[-1][0] == renamed
