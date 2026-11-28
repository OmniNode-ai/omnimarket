"""The OMN-25104 contract retires the bindings of the criteria its ticket dropped.

The ticket now carries AC1, AC2, AC3 and AC5. The omnimarket contract still holds
items bound to AC4 and AC7, which the ticket no longer carries, so dod_verify
refuses them as CRITERIA_DRIFT and the Done write sees a check that is not
verified. A ``supersedes_ac_binding`` entry per deleted label retires each claim.
AC3 is declared by the omnidash contract, so it is not read here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)
from omnimarket.occ_contract_pin import criterion_hash

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-25104.yaml"
_LIVE_CRITERIA = (
    "## Acceptance criteria\n"
    "- [ ] AC1: a compute node folds ledger events into done items\n"
    "- [ ] AC2: replaying the same ledger events publishes the same projection\n"
    "- [ ] AC5: the integration-proof item starts with a program a lab host runs\n"
)
_DELETED = {"AC4": "dod-omn25104-ac4", "AC7": "dod-omn25104-ac7"}


def _items() -> list[dict[str, object]]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return list(document["dod_evidence"])


def _accepted(label: str) -> dict[str, str]:
    return {
        "label": label,
        "criterion_hash": criterion_hash(f"{label}: accepted"),
        "proposed_by": "occ-autobind",
        "accepted_by": "reviewer-lane",
    }


def test_no_deleted_criterion_is_left_unretired() -> None:
    # Each item carries the accepted binding of the label it claims, as the
    # change-control record does for the live contract.
    items = [
        {**item, "ac_bindings": [_accepted(label) for label in item["binds_ac"]]}  # type: ignore[attr-defined]
        if item.get("binds_ac")
        else item
        for item in _items()
    ]
    pins = extract_criteria_pins({}, items)
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-25104", _LIVE_CRITERIA)
    deleted = [d.label for d in drift if d.kind is EnumCriteriaDriftKind.DELETED]
    assert deleted == [], deleted


def test_each_deleted_label_is_retired_by_a_typed_entry() -> None:
    retirements = [
        entry
        for item in _items()
        for entry in item.get("supersedes_ac_binding", [])  # type: ignore[attr-defined]
    ]
    retired = {e["label"]: e["item"] for e in retirements}
    assert retired == _DELETED
    for entry in retirements:
        assert entry["reason_kind"] == "no_longer_applicable"
        assert entry["retired_by"]
        assert entry["retired_at"]
        assert entry["reason"]
