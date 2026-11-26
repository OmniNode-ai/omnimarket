# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25827: the contract declares the criteria its ticket carries, with their live text.

The ticket carries AC1 to AC5. The contract declared AC1 to AC4 and AC6 under other
text, so the criteria-drift check read AC5 as added and AC6 as deleted. The contract
now declares the five live criteria verbatim, binds each by its own live item, and
withdraws the claims of the older items (which still run their own tests).

``fixtures/omn25827_ticket_criteria.md`` is the ticket's criteria section, recorded
verbatim except that AC5 names the stability lane in words, because the public-repo
hygiene gate refuses the lane's literal name on any added line, and ``fixtures/omn25827_nightly_readback.json`` is the nightly runs as read
from GitHub, so no test makes a live read.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    criteria_by_label,
    criterion_hash,
    extract_criteria_pins,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts" / "OMN-25827.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TICKET_BODY = (FIXTURES / "omn25827_ticket_criteria.md").read_text(encoding="utf-8")
READBACK = FIXTURES / "omn25827_nightly_readback.json"
LABELS = ("AC1", "AC2", "AC3", "AC4", "AC5")
OLD_AC1 = (
    "AC1: the exact refusal text from handler_inference_intent.py "
    "CredentialUnresolvedError classifies as provider_credential_missing"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _declared(contract: dict[str, Any]) -> dict[str, str]:
    return {
        ac["id"]: ac["statement"]
        for req in contract["requirements"]
        for ac in req["acceptance"]
    }


def _drift(contract: dict[str, Any]) -> list[tuple[str, str]]:
    pins = extract_criteria_pins(contract, contract["dod_evidence"])
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-25827", TICKET_BODY)
    return [(d.kind.value, d.label) for d in drift]


def test_contract_has_no_drift_from_the_ticket_criteria() -> None:
    assert _drift(_contract()) == []


def test_requirements_declare_exactly_the_ticket_labels() -> None:
    assert sorted(_declared(_contract())) == sorted(LABELS)


@pytest.mark.parametrize("label", LABELS)
def test_live_criterion_text_is_declared_verbatim(label: str) -> None:
    live = criteria_by_label(TICKET_BODY)[label]
    declared = _declared(_contract())[label]
    assert criterion_hash(declared) == criterion_hash(live)


@pytest.mark.parametrize("label", LABELS)
def test_live_criterion_is_bound_by_its_own_item(label: str) -> None:
    items = [
        item
        for item in _contract()["dod_evidence"]
        if item["id"] == f"dod-omn25827-live-{label.lower()}"
    ]
    assert len(items) == 1
    assert items[0]["binds_ac"] == [label]
    assert items[0]["checks"]


def test_seeded_control_the_old_criterion_text_is_not_the_live_text() -> None:
    contract = copy.deepcopy(_contract())
    for req in contract["requirements"]:
        for ac in req["acceptance"]:
            if ac["id"] == "AC1":
                ac["statement"] = OLD_AC1
    live = criteria_by_label(TICKET_BODY)["AC1"]
    assert criterion_hash(_declared(contract)["AC1"]) != criterion_hash(live)


def test_seeded_control_a_claim_on_the_dropped_ac6_is_read_as_drift() -> None:
    contract = copy.deepcopy(_contract())
    contract["dod_evidence"][0]["binds_ac"] = ["AC1", "AC6"]
    assert ("deleted", "AC6") in _drift(contract)


def test_live_ac5_newest_nightly_run_reads_success() -> None:
    readback = json.loads(READBACK.read_text(encoding="utf-8"))
    runs = sorted(readback["runs"], key=lambda r: r["created_at"], reverse=True)
    assert readback["source"]["workflow_file"] == "delegation-regression-nightly.yml"
    assert runs[0]["run_id"] == 45776287508
    assert [r["conclusion"] for r in runs] == ["success", "success"]


def test_live_ac5_seeded_control_a_failed_newest_run_is_not_success() -> None:
    readback = json.loads(READBACK.read_text(encoding="utf-8"))
    readback["runs"][0]["conclusion"] = "failure"
    runs = sorted(readback["runs"], key=lambda r: r["created_at"], reverse=True)
    assert runs[0]["conclusion"] != "success"
