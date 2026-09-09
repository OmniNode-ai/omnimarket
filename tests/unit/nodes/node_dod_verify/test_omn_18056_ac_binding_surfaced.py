# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18056 — the contract's AC binding must reach the receipt.

The evidence autoclose sweep could ask a green verdict only how many checks
passed, never which acceptance criterion any of them covered. Measured in DoD
closeout sweep run 2 (2026-09-08): 8 of 9 adjudicated sprint tickets satisfied
the closer's full flip predicate and 7 of those 8 were not done — in every held
case the criterion that decides the ticket was bound to no check in its OCC
contract.

The verifier is the only place the binding can be reported from without adding
a second contract parser: it already resolves, pins and reads the contract, and
the sweep runs on a runner with no contract checkout. This module pins that the
declaration survives the trip from `dod_evidence[].binds_ac` to
`ModelEvidenceCheckResult.binds_ac`, including onto the OMN-14207 live-PR-state
overlay an item produces.

RED before the change: `ModelEvidenceCheckResult` had no `binds_ac` field at
all, so every assertion below raised `AttributeError`, and a contract carrying
`binds_ac` was rejected wholesale by the strict item-field-set gate as
`INVALID_DOD_EVIDENCE_ITEM ... unknown field(s): 'binds_ac'`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit


def _contract(tmp_path: Path, items: list[dict[str, object]]) -> str:
    path = tmp_path / "OMN-9999.yaml"
    path.write_text(
        yaml.safe_dump({"ticket_id": "OMN-9999", "dod_evidence": items}),
        encoding="utf-8",
    )
    return str(path)


def test_a_declared_binding_reaches_every_result_for_that_item(
    tmp_path: Path,
) -> None:
    """THE POINT OF THE CHANGE: the author's claim survives to the consumer."""
    path = _contract(
        tmp_path,
        [
            {
                "id": "dod-tests",
                "description": "both call sites are scoped",
                "binds_ac": ["AC2", "AC3"],
                "checks": [{"check_type": "command", "check_value": "true"}],
            },
            {
                "id": "dod-provenance",
                "description": "the companion merged",
                "checks": [{"check_type": "command", "check_value": "true"}],
            },
        ],
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    by_id = {result.evidence_id.split("::", 1)[0]: result for result in results}
    assert by_id["dod-tests"].binds_ac == ("AC2", "AC3")
    # An item that declares nothing declares NOTHING — never everything.
    assert by_id["dod-provenance"].binds_ac == ()


def test_an_undeclared_binding_is_empty_not_absent(tmp_path: Path) -> None:
    """The corpus default. 8709 contracts declare no binding today.

    Empty is the honest report of "this item claims to cover no criterion",
    which a consumer reads as a coverage gap. It must never be conflated with
    the receipt not carrying the field at all, which means the verifier cannot
    answer the question — a different fact with a different repair.
    """
    path = _contract(
        tmp_path,
        [
            {
                "id": "dod-1",
                "description": "x",
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        ],
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert results
    assert all(result.binds_ac == () for result in results)
    # Present-and-empty, not absent: the key is on the serialised receipt.
    assert "binds_ac" in results[0].model_dump()


def test_a_contract_declaring_a_binding_is_not_rejected_as_unknown_field(
    tmp_path: Path,
) -> None:
    """The strict item-field-set gate must ACCEPT the new canonical field.

    Before this change `binds_ac` was an unknown field, and the gate refuses
    to execute any check from an audience-ambiguous contract — so the first
    contract to declare a binding would have failed wholesale rather than
    binding anything.
    """
    path = _contract(
        tmp_path,
        [
            {
                "id": "dod-1",
                "description": "x",
                "binds_ac": ["AC1"],
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        ],
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert not any(
        "INVALID_DOD_EVIDENCE_ITEM" in (result.message or "") for result in results
    )
    assert results[0].binds_ac == ("AC1",)
