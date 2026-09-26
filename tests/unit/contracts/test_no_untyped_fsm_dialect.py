# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No contract declares the untyped ``fsm:`` dialect (OMN-19548, epic OMN-19546).

Golden-chain validation layer plan r4, Phase -1 deliverable 1: one FSM dialect.
The untyped ``fsm:`` block (keys ``from``/``to``, prose triggers) cannot load
into ``ModelFSMSubcontract`` and nothing executes it. A contract machine is
declared as a typed ``state_machine:`` block; see
``tests/unit/contracts/test_walkable_state_machines.py`` for what that form
must satisfy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_SRC = Path(__file__).resolve().parents[3] / "src"


def _untyped_dialect_contracts(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("contract.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "fsm" in data:
            found.append(str(path.relative_to(root)))
    return found


@pytest.mark.unit
def test_no_contract_declares_the_untyped_fsm_dialect() -> None:
    found = _untyped_dialect_contracts(_SRC)
    assert not found, (
        "these contracts declare the untyped fsm: dialect; declare a typed "
        f"state_machine: block instead: {found}"
    )


@pytest.mark.unit
def test_scan_finds_an_untyped_block_when_one_exists(tmp_path: Path) -> None:
    """Positive control: the scan is not vacuously empty."""
    node = tmp_path / "node_fixture"
    node.mkdir()
    (node / "contract.yaml").write_text(
        "name: node_fixture\nfsm:\n  states: [A, B]\n  initial_state: A\n"
        "  transitions:\n    - {from: A, to: B, trigger: go}\n",
        encoding="utf-8",
    )
    assert _untyped_dialect_contracts(tmp_path) == ["node_fixture/contract.yaml"]
