# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger sequencing contracts keep every criterion bound to hosted evidence (OMN-24825).

A criterion whose only bound evidence is a local_done_gate item is refused by the hosted
receipt gate: the gate runs such an item neither at the head nor at the merge base, so its
control cannot run. The four contracts of this series therefore bind each such criterion to a
hosted pytest item and keep the local reading as an unbound local_done_gate item. This test pins
both halves, so a later edit can neither drop a criterion nor bind one back to a local-only item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"

EXPECTED_CRITERIA = {
    "OMN-24634": ["AC1", "AC2", "AC3", "AC4", "AC5"],
    "OMN-24644": ["AC1", "AC2", "AC3"],
    "OMN-24662": ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"],
    "OMN-24683": ["AC1", "AC2", "AC3", "AC4"],
}


def _load(ticket: str) -> dict[str, Any]:
    loaded = yaml.safe_load((CONTRACTS / f"{ticket}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("ticket", sorted(EXPECTED_CRITERIA))
def test_the_contract_keeps_every_criterion(ticket: str) -> None:
    contract = _load(ticket)
    criteria = [
        ac["id"] for req in contract["requirements"] for ac in req["acceptance"]
    ]
    assert criteria == EXPECTED_CRITERIA[ticket]


@pytest.mark.parametrize("ticket", sorted(EXPECTED_CRITERIA))
def test_every_criterion_is_bound_to_a_hosted_item(ticket: str) -> None:
    contract = _load(ticket)
    hosted_bound = {
        ac_id
        for item in contract["dod_evidence"]
        if item.get("execution_scope") != "local_done_gate"
        for ac_id in item.get("binds_ac") or []
    }
    assert set(EXPECTED_CRITERIA[ticket]) <= hosted_bound


@pytest.mark.parametrize("ticket", sorted(EXPECTED_CRITERIA))
def test_no_local_done_gate_item_binds_a_criterion(ticket: str) -> None:
    contract = _load(ticket)
    offenders = [
        item["id"]
        for item in contract["dod_evidence"]
        if item.get("execution_scope") == "local_done_gate" and item.get("binds_ac")
    ]
    assert offenders == []
