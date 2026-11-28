"""The OMN-25103 contract retires the binding of the criterion its ticket dropped.

The ticket now carries AC1 to AC6. The omnimarket contract still holds the item
``dod-omn25103-user-only-provision`` bound to AC7, which the ticket no longer
carries, so dod_verify refuses it as CRITERIA_DRIFT and the Done write sees a check
that is not verified. A ``supersedes_ac_binding`` entry for the deleted label
retires that claim. AC2, AC3 and AC5 are declared by the omnidash and omnibase_infra
contracts, so they are not read here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)
from omnimarket.occ_contract_pin import criterion_hash

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-25103.yaml"
_LIVE_CRITERIA = (
    "## Acceptance criteria\n"
    "- [ ] AC1: a projection writer serves the newest pass's rows and counts\n"
    "- [ ] AC4: one integration pass publishes both the snapshot and the event\n"
    "- [ ] AC6: the integration-proof item starts with a program a lab host runs\n"
)


def _document() -> dict[str, Any]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _items() -> list[dict[str, Any]]:
    return list(_document()["dod_evidence"])


def _accepted(label: str) -> dict[str, str]:
    return {
        "label": label,
        "criterion_hash": criterion_hash(f"{label}: accepted"),
        "proposed_by": "occ-autobind",
        "accepted_by": "reviewer-lane",
    }


def test_no_deleted_criterion_is_left_unretired() -> None:
    items = [
        {**item, "ac_bindings": [_accepted(label) for label in item["binds_ac"]]}
        if item.get("binds_ac")
        else item
        for item in _items()
    ]
    pins = extract_criteria_pins({}, items)
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-25103", _LIVE_CRITERIA)
    deleted = [d.label for d in drift if d.kind is EnumCriteriaDriftKind.DELETED]
    assert deleted == [], deleted


def test_ac7_is_retired_by_a_typed_entry() -> None:
    retirements = [
        entry for item in _items() for entry in item.get("supersedes_ac_binding", [])
    ]
    assert {e["label"]: e["item"] for e in retirements} == {
        "AC7": "dod-omn25103-user-only-provision"
    }
    for entry in retirements:
        assert entry["reason_kind"] == "no_longer_applicable"
        assert entry["retired_by"]
        assert entry["retired_at"]
        assert entry["reason"]


def test_the_contract_declares_no_ac7_requirement() -> None:
    labels = [
        acceptance["id"]
        for requirement in _document()["requirements"]
        for acceptance in requirement["acceptance"]
    ]
    assert "AC7" not in labels, labels
