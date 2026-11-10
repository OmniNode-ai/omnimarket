# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23278: batch 4 of the OMN-17427 contract-only retirement is retired whole.

One change that retired every contract-only label made ``repo-evidence / dod-verify``
run the 227 checks of the retired items (about 54 minutes) against the 43 minute
window of ``Repo Evidence Dependency``. The retirement is five batches of at most 40
labels. Each batch retires its labels by its own carrier item, so no carrier
references another batch's items, and each pull request cites OMN-23278, whose
contract runs only the batch tests.

Batch 4 is retired when its carrier item exists, no label of the batch is still
declared by a requirement, and the carrier withdraws the claim of every item that
binds a label of the batch (and of no other label).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-17427.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BATCH = 4
CARRIER_ID = f"dod-omn-17427-contract-only-retirement-batch-{BATCH}"
MAX_LABELS_PER_BATCH = 40
# The window of ``Repo Evidence Dependency``: 2580 seconds, never changed here.
EVIDENCE_WINDOW_LINE = "deadline=2580"

LABELS: tuple[str, ...] = (
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
)


def batch_problems(
    contract: Mapping[str, Any], labels: tuple[str, ...] = LABELS
) -> list[str]:
    """Every way the contract holds the batch half retired, empty when it is retired whole."""
    items = contract.get("dod_evidence") or []
    carrier = next((i for i in items if i.get("id") == CARRIER_ID), None)
    if carrier is None:
        return [f"no carrier item {CARRIER_ID}"]
    declared = {
        str(entry["id"])
        for requirement in contract.get("requirements") or []
        for entry in requirement.get("acceptance") or []
    }
    problems = [
        f"label still declared: {label}" for label in labels if label in declared
    ]
    entries = carrier.get("supersedes_ac_binding") or []
    retired = {
        (str(e.get("item")), str(e.get("label")))
        for e in entries
        if e.get("reason_kind") == "no_longer_applicable"
    }
    problems += [
        f"{item.get('id')} {label} not retired"
        for item in items
        for label in item.get("binds_ac") or []
        if label in labels and (str(item.get("id")), label) not in retired
    ]
    problems += [
        f"carrier retires a label outside the batch: {e.get('label')}"
        for e in entries
        if e.get("label") not in labels
    ]
    return problems


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _synthetic(
    *, carrier: bool, declared: tuple[str, ...], entries: tuple[tuple[str, str], ...]
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "requirements": [{"id": "r", "acceptance": [{"id": x} for x in declared]}],
        "dod_evidence": [{"id": "target", "binds_ac": [LABELS[0]]}],
    }
    if carrier:
        contract["dod_evidence"].append(
            {
                "id": CARRIER_ID,
                "supersedes_ac_binding": [
                    {"item": i, "label": x, "reason_kind": "no_longer_applicable"}
                    for i, x in entries
                ],
            }
        )
    return contract


def test_contract_only_retirement_batch4_detector_reads_a_declared_label() -> None:
    """Named injected break: the carrier is present while a batch label is still declared."""
    contract = _synthetic(
        carrier=True, declared=(LABELS[0],), entries=(("target", LABELS[0]),)
    )
    assert any("still declared" in p for p in batch_problems(contract))


def test_contract_only_retirement_batch4_detector_reads_an_unretired_claim() -> None:
    """Named injected break: a carrier that leaves an item's claim on a batch label."""
    contract = _synthetic(carrier=True, declared=(), entries=())
    assert any("not retired" in p for p in batch_problems(contract))


def test_contract_only_retirement_batch4_detector_reads_a_foreign_label() -> None:
    """Named injected break: a carrier that retires a label another batch owns."""
    contract = _synthetic(
        carrier=True,
        declared=(),
        entries=(("target", LABELS[0]), ("other", "AC-not-in-this-batch")),
    )
    assert any("outside the batch" in p for p in batch_problems(contract))


def test_contract_only_retirement_batch4_detector_reads_a_missing_carrier() -> None:
    """Named injected break: the labels leave the requirements and no carrier retires them."""
    contract = _synthetic(carrier=False, declared=(), entries=())
    assert batch_problems(contract) == [f"no carrier item {CARRIER_ID}"]


def test_contract_only_retirement_batch4_holds_at_most_forty_labels() -> None:
    assert len(LABELS) == len(set(LABELS))
    assert len(LABELS) <= MAX_LABELS_PER_BATCH


def test_contract_only_retirement_batch4_is_retired_whole() -> None:
    """Fails at a tree without the batch: no carrier item exists there."""
    assert batch_problems(_contract()) == []


def test_contract_only_retirement_batch4_carrier_runs_no_check_a_retired_item_runs() -> (
    None
):
    items = _contract()["dod_evidence"]
    by_id = {str(item.get("id")): item for item in items}
    carrier = by_id[CARRIER_ID]

    def key(check: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return (check.get("check_type"), check.get("check_value"), check.get("cwd"))

    own = {key(c) for c in carrier.get("checks") or []}
    repeated = [
        f"{CARRIER_ID} repeats {entry.get('item')}"
        for entry in carrier.get("supersedes_ac_binding") or []
        for c in by_id.get(str(entry.get("item")), {}).get("checks") or []
        if key(c) in own
    ]
    assert not repeated, repeated[:3]


def test_contract_only_retirement_batch4_evidence_window_bound_is_unchanged() -> None:
    assert EVIDENCE_WINDOW_LINE in CI_WORKFLOW.read_text(encoding="utf-8")
