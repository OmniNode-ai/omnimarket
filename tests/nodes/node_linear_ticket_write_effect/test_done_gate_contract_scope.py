# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket closed over the bus carries no evidence item the hosted done gate skips.

The consumer behind ``onex ticket transition`` runs the done gate's verifier for the
hosted audience. A ``local_done_gate`` item is skipped there, and the gate needs
verified checks to equal total checks, so one such item refuses the Done write with
``dod_verify ... is not a PASS`` (the reading for the tenant provisioning ticket was
16 verified of 17).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
_CLOSED_OVER_THE_BUS = ("OMN-26473",)


@pytest.mark.parametrize("ticket", _CLOSED_OVER_THE_BUS)
def test_hosted_gate_skips_no_item_of_a_bus_closed_ticket(ticket: str) -> None:
    contract = yaml.safe_load((_CONTRACTS / f"{ticket}.yaml").read_text("utf-8"))
    skipped = [
        item["id"]
        for item in contract["dod_evidence"]
        if item.get("execution_scope") == "local_done_gate"
    ]
    assert contract["dod_evidence"], "the positive control: the contract has items"
    assert skipped == []
