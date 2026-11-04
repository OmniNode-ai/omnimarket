# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_reducer: the contract's states are the entry-state enum's, and each is reachable.

The node's own tests live beside it (``src/omnimarket/nodes/node_floor_fill_reducer/tests``);
the contract-state-coverage gate reads this directory, so the states are asserted here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    VALID_TRANSITIONS,
)
from omnimarket.nodes.node_floor_fill_reducer.models import EnumFloorFillEntryState

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_floor_fill_reducer"
    / "contract.yaml"
)


def test_the_contract_states_are_the_entry_states() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = {s["state_name"] for s in contract["state_machine"]["states"]}
    assert declared == {"NONE", "RESERVED", "SENT"}
    assert declared == {s.value.upper() for s in EnumFloorFillEntryState}


def test_every_state_is_entered_and_left() -> None:
    entered = set(VALID_TRANSITIONS.values())
    left = {frm for frm, _ in VALID_TRANSITIONS}
    assert entered == {"none", "reserved", "sent"}
    assert left == {"none", "reserved", "sent"}
