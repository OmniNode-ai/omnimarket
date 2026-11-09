# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23278: the OMN-17427 contract-only retirement lands in batches of at most 40 labels.

One change that retired every contract-only label made ``repo-evidence / dod-verify``
run the 227 checks of the retired items (about 54 minutes) against the 43 minute
window of ``Repo Evidence Dependency``. The retirement is five batches; each retires
its labels by its own carrier item, so no carrier references another batch's items,
and each pull request cites OMN-23278, whose contract holds only the checks below.

A batch is either retired whole or not at all: its carrier item exists, no label of
the batch is still declared by a requirement, and the carrier withdraws the claim of
every item that binds a label of the batch; or its carrier is absent and every label
of the batch is still declared. The same file is carried by all five batches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-17427.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CARRIER_PREFIX = "dod-omn-17427-contract-only-retirement-batch-"
MAX_LABELS_PER_BATCH = 40
# The window of ``Repo Evidence Dependency``: 2580 seconds, never changed here.
EVIDENCE_WINDOW_LINE = "deadline=2580"

BATCH_LABELS: dict[int, tuple[str, ...]] = {
    1: (
        "AC3003101",
        "AC3003102",
        "AC3003104",
        "AC3003103",
        "AC1742701",
        "AC161",
        "AC5",
        "AC6",
        "AC8",
        "AC7",
        "AC9",
        "AC10",
        "AC11",
        "AC12",
        "AC14",
        "AC15",
        "AC16",
        "AC17",
        "AC18",
        "AC19",
        "AC20",
        "AC21",
        "AC22",
        "AC23",
        "AC24",
        "AC33",
        "AC25",
        "AC26",
        "AC27",
        "AC28",
        "AC29",
        "AC30",
        "AC31",
        "AC32",
        "AC34",
    ),
    2: (
        "AC35",
        "AC36",
        "AC37",
        "AC38",
        "AC39",
        "AC40",
        "AC41",
        "AC42",
        "AC43",
        "AC44",
        "AC46",
        "AC47",
        "AC48",
        "AC49",
        "AC52",
        "AC53",
        "AC58",
        "AC59",
        "AC50",
        "AC51",
        "AC54",
        "AC55",
        "AC56",
        "AC57",
        "AC60",
        "AC61",
        "AC72",
        "AC73",
        "AC74",
        "AC62",
        "AC70",
        "AC71",
        "AC81",
        "AC64",
        "AC63",
    ),
    3: (
        "AC65",
        "AC66",
        "AC76",
        "AC67",
        "AC68",
        "AC80",
        "AC69",
        "AC101",
        "AC456401",
        "AC456402",
        "AC184",
        "AC75",
        "AC77",
        "AC105",
        "AC107",
        "AC109",
        "AC78",
        "AC92",
        "AC79",
        "AC91",
        "AC94",
        "AC82",
        "AC83",
        "AC93",
        "AC95",
        "AC96",
        "AC97",
        "AC106",
        "AC151",
        "AC98",
        "AC99",
        "AC100",
        "AC102",
        "AC103",
        "AC142",
    ),
    4: (
        "AC111",
        "AC112",
        "AC108",
        "AC183",
        "AC141",
        "AC140",
        "AC113",
        "AC150",
        "AC152",
        "AC130",
        "AC131",
        "AC158",
        "AC155",
        "AC156",
        "AC153",
        "AC154",
        "AC157",
        "AC114",
        "AC159",
        "AC160",
        "AC163",
        "AC164",
        "AC162",
        "AC180",
        "AC181",
        "AC182",
        "AC145",
        "AC185",
        "AC186",
        "AC187",
        "AC4301",
        "AC188",
        "AC189",
        "AC190",
        "AC4303",
    ),
    5: (
        "AC4351",
        "AC4352",
        "AC4353",
        "AC4354",
        "AC4357",
        "AC4400",
        "AC4401",
        "AC21288",
        "AC4543",
        "AC4544",
        "AC1742711",
        "AC1742712",
        "AC1742713",
        "AC1742714",
        "AC1742715",
        "AC1742716",
        "AC1742721",
        "AC1742722",
        "AC1742723",
        "AC1742724",
        "AC1742725",
        "AC460301",
        "AC460302",
        "AC460303",
        "AC1742726",
        "AC1742727",
        "AC1742728",
        "AC1742729",
        "AC1742730",
        "AC1742731",
        "AC1742732",
        "AC1742733",
        "AC513401",
        "AC3003201",
        "AC1742790",
        "AC2249601",
    ),
}


def declared_labels(contract: Mapping[str, Any]) -> set[str]:
    """Labels a requirement of the contract still declares."""
    return {
        str(entry["id"])
        for requirement in contract.get("requirements") or []
        for entry in requirement.get("acceptance") or []
    }


def batch_problems(contract: Mapping[str, Any]) -> list[str]:
    """Every way the contract holds a batch half retired, empty when each batch is whole."""
    items = contract.get("dod_evidence") or []
    carriers = {
        str(item.get("id")): item
        for item in items
        if str(item.get("id", "")).startswith(CARRIER_PREFIX)
    }
    declared = declared_labels(contract)
    problems: list[str] = []
    for number, labels in BATCH_LABELS.items():
        carrier = carriers.get(f"{CARRIER_PREFIX}{number}")
        if carrier is None:
            still = [label for label in labels if label not in declared]
            if still:
                problems.append(
                    f"batch {number}: no carrier, labels undeclared: {still[:3]}"
                )
            continue
        gone = [label for label in labels if label in declared]
        if gone:
            problems.append(f"batch {number}: labels still declared: {gone[:3]}")
        retired = {
            (str(entry.get("item")), str(entry.get("label")))
            for entry in carrier.get("supersedes_ac_binding") or []
            if entry.get("reason_kind") == "no_longer_applicable"
        }
        for item in items:
            for label in item.get("binds_ac") or []:
                if label in labels and (str(item.get("id")), label) not in retired:
                    problems.append(
                        f"batch {number}: {item.get('id')} {label} not retired"
                    )
    return problems


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _synthetic(
    *, carrier: bool, declared: Sequence[str], entries: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    first = BATCH_LABELS[1][0]
    contract: dict[str, Any] = {
        "requirements": [
            {"id": "r", "acceptance": [{"id": label} for label in declared]}
        ],
        "dod_evidence": [{"id": "target", "binds_ac": [first]}],
    }
    if carrier:
        contract["dod_evidence"].append(
            {
                "id": f"{CARRIER_PREFIX}1",
                "supersedes_ac_binding": [
                    {
                        "item": item,
                        "label": label,
                        "reason_kind": "no_longer_applicable",
                    }
                    for item, label in entries
                ],
            }
        )
    return contract


def test_contract_only_detector_reads_a_declared_label_of_a_retired_batch() -> None:
    """Named injected break: a carrier present while a batch label is still declared."""
    rest = [
        label
        for batch, labels in BATCH_LABELS.items()
        if batch != 1
        for label in labels
    ]
    contract = _synthetic(
        carrier=True,
        declared=[BATCH_LABELS[1][0], *rest],
        entries=[("target", BATCH_LABELS[1][0])],
    )
    assert any(
        "labels still declared" in problem for problem in batch_problems(contract)
    )


def test_contract_only_detector_reads_an_unretired_claim() -> None:
    """Named injected break: a carrier that leaves an item's claim on a batch label."""
    rest = [
        label
        for batch, labels in BATCH_LABELS.items()
        if batch != 1
        for label in labels
    ]
    contract = _synthetic(carrier=True, declared=rest, entries=[])
    assert any("not retired" in problem for problem in batch_problems(contract))


def test_contract_only_detector_reads_labels_dropped_without_a_carrier() -> None:
    """Named injected break: a batch's labels leave the requirements and no carrier retires them."""
    rest = [
        label
        for batch, labels in BATCH_LABELS.items()
        if batch != 1
        for label in labels
    ]
    contract = _synthetic(carrier=False, declared=rest, entries=[])
    assert any("no carrier" in problem for problem in batch_problems(contract))


def test_contract_only_detector_reads_whole_batches_as_clean() -> None:
    everything = [label for labels in BATCH_LABELS.values() for label in labels]
    contract = _synthetic(carrier=False, declared=everything, entries=[])
    assert batch_problems(contract) == []


def test_contract_only_batches_hold_at_most_forty_labels_and_overlap_nowhere() -> None:
    every = [label for labels in BATCH_LABELS.values() for label in labels]
    assert len(every) == len(set(every)), "a label is named by two batches"
    assert all(len(labels) <= MAX_LABELS_PER_BATCH for labels in BATCH_LABELS.values())


def test_contract_only_each_batch_is_retired_whole_or_not_at_all() -> None:
    assert batch_problems(_contract()) == []


def test_contract_only_retirement_has_begun() -> None:
    """Fails at a tree without any batch: no carrier item exists there."""
    items = _contract()["dod_evidence"]
    assert any(str(item.get("id", "")).startswith(CARRIER_PREFIX) for item in items)


def test_contract_only_carriers_run_no_check_a_retired_item_runs() -> None:
    items = _contract()["dod_evidence"]
    by_id = {str(item.get("id")): item for item in items}
    repeated: list[str] = []
    for carrier in items:
        if not str(carrier.get("id", "")).startswith(CARRIER_PREFIX):
            continue
        own = {
            (c.get("check_type"), c.get("check_value"), c.get("cwd"))
            for c in carrier.get("checks") or []
        }
        for entry in carrier.get("supersedes_ac_binding") or []:
            target = by_id.get(str(entry.get("item")), {})
            repeated.extend(
                f"{carrier['id']} repeats {target.get('id')}"
                for c in target.get("checks") or []
                if (c.get("check_type"), c.get("check_value"), c.get("cwd")) in own
            )
    assert not repeated, repeated[:3]


def test_contract_only_evidence_window_bound_is_unchanged() -> None:
    assert EVIDENCE_WINDOW_LINE in CI_WORKFLOW.read_text(encoding="utf-8")
