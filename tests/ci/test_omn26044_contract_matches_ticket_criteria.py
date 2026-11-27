# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26044: the contract binds the criteria its ticket carries, no more and no fewer.

The ticket carries AC1 to AC5. The contract once bound AC1, AC2, AC4, AC5, AC6 and AC7,
so the criteria-drift check read AC6 and AC7 as deleted from the ticket and AC3 as added.
The contract now declares AC1 to AC5, binds AC3 by its own item, and withdraws the claims
of the two criteria the ticket never carried (their items still run their own tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26044.yaml"
TICKET_LABELS = ("AC1", "AC2", "AC3", "AC4", "AC5")
TICKET_BODY = "## Acceptance criteria\n\n" + "".join(
    f"- [ ] {label}: criterion text -- falsifier: recorded\n" for label in TICKET_LABELS
)


def _drift(contract: dict[str, object]) -> list[str]:
    items = contract["dod_evidence"]
    assert isinstance(items, list)
    pins = extract_criteria_pins(contract, items)
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-26044", TICKET_BODY)
    return [d.summary() for d in drift]


def test_contract_has_no_drift_from_the_ticket_criteria() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert _drift(contract) == []


def test_requirements_declare_exactly_the_ticket_labels() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    declared = [
        ac["id"] for req in contract["requirements"] for ac in req["acceptance"]
    ]
    assert sorted(declared) == sorted(TICKET_LABELS)


def test_every_ticket_label_has_a_bound_item() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    bound = {
        label
        for item in contract["dod_evidence"]
        for label in item.get("binds_ac") or []
    }
    assert set(TICKET_LABELS) <= bound


def test_seeded_control_a_stray_label_is_read_as_drift() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["dod_evidence"][0]["binds_ac"] = ["AC1", "AC9"]
    assert any("AC9" in line for line in _drift(contract))
