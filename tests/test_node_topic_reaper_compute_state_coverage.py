# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""State-coverage contract test for node_topic_reaper_compute (OMN-21594).

Asserts the reaper FSM's declared states against the contract and the state enum
the handler emits, so the strict state-coverage gate sees each state literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_topic_reaper_compute as _reaper_node_pkg
from omnimarket.models.model_topic_reaper import (
    EnumReaperState,
)

_CONTRACT_PATH = Path(_reaper_node_pkg.__file__).parent / "contract.yaml"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("enum_member", "state_name"),
    [
        (EnumReaperState.ACTIVE, "active"),
        (EnumReaperState.FLAGGED, "flagged"),
        (EnumReaperState.ELIGIBLE, "eligible"),
        (EnumReaperState.DELETING, "deleting"),
        (EnumReaperState.DELETED, "deleted"),
    ],
)
def test_reaper_state_enum_matches_declared_state(
    enum_member: EnumReaperState, state_name: str
) -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    declared = [s["state_name"] for s in contract["state_machine"]["states"]]
    assert str(enum_member) == state_name
    assert state_name in declared
