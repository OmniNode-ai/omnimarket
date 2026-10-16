# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20072: the contract claims only criteria the ticket still carries.

The ticket's scope revision of 2026-10-02 superseded its AC1 and AC2 and kept
AC3 to AC6. The contract's ``dod-s5-case-list`` item still binds AC1, so the
verifier's criteria check refuses the whole contract ``CRITERIA_DRIFT`` (AC1
deleted from the ticket). The item is rebound in place to AC3, whose clause that
a difference with no reason code fails the check is what the case list shows.

The rebind is not a ``supersedes_ac_binding`` retirement: the verifier reads that
field's schema from an onex_change_control tree, and the hosted
``repo-evidence / dod-verify`` job has none, so it would refuse the contract
outright. Every item field must be one the verifier owns on its own.

``fixtures/omn20072_ticket_criteria.md`` is the ticket's criteria section,
recorded verbatim, so no test makes a live ticket read.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    criteria_by_label,
    extract_criteria_pins,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _LOCAL_DOD_ITEM_FIELDS,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts" / "OMN-20072.yaml"
TICKET_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omn20072_ticket_criteria.md"
)
CASE_LIST_CHECK = "uv run pytest tests/test_dod_verify_s5_case_list.py -q"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _drift(contract: dict[str, Any]) -> list[tuple[str, str]]:
    items = contract["dod_evidence"]
    pins = extract_criteria_pins(contract, items)
    assert pins is not None, "the contract records no criteria to compare"
    drift, _ = compare_criteria(
        pins, "OMN-20072", TICKET_PATH.read_text(encoding="utf-8")
    )
    return [(d.kind.value, d.label) for d in drift]


def test_contract_has_no_criteria_drift_against_the_ticket() -> None:
    assert _drift(_contract()) == []


def test_a_binding_to_the_superseded_ac1_is_reported_deleted() -> None:
    contract = copy.deepcopy(_contract())
    for item in contract["dod_evidence"]:
        if item["id"] == "dod-s5-case-list":
            item["binds_ac"] = ["AC1"]
    assert _drift(contract) == [(EnumCriteriaDriftKind.DELETED.value, "AC1")]


def test_case_list_check_stays_bound_to_a_live_criterion() -> None:
    contract = _contract()
    live = frozenset(criteria_by_label(TICKET_PATH.read_text(encoding="utf-8")))
    bound = {
        label
        for item in contract["dod_evidence"]
        for label in item.get("binds_ac", [])
        if any(c["check_value"] == CASE_LIST_CHECK for c in item["checks"])
    }
    assert bound == {"AC3"} <= live


def test_every_item_field_is_owned_by_the_verifier_without_an_occ_tree() -> None:
    for item in _contract()["dod_evidence"]:
        assert set(item) <= _LOCAL_DOD_ITEM_FIELDS, item["id"]
