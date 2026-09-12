# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18236 — the verifier accepts the per-criterion binding record.

`binds_ac` on an evidence item is the author's CLAIM about which acceptance
criterion the item proves. OMN-18236 adds the record that turns a claim into a
binding: `ac_bindings`, one entry per criterion, carrying the hash of the
criterion text it was pinned to and, when somebody has agreed to it, who
accepted it and when. Without the hash a criterion can be rewritten under a
check that is still green; without the acceptance a machine-proposed mapping and
a reviewed one are indistinguishable.

That record is OCC-local. `ModelContractDodItem` does not carry it and is not
expected to, so this consumer has to name it explicitly — and the consequence of
not naming it is not a missing feature, it is a hard failure. This collector
refuses an item carrying any field outside its canonical set with
`INVALID_DOD_EVIDENCE_ITEM`, and that refusal fails EVERY evidence check on the
contract. So the first contract ever to record a binding would have had every
one of its checks fail, for carrying the record that proves its criterion.

RED before the change: `test_a_contract_recording_a_binding_is_not_rejected`
failed with `INVALID_DOD_EVIDENCE_ITEM: strict canonical field set rejected
unknown field(s): 'ac_bindings'=...`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_HASH = "9d006777c2e97aabd867d0c48c23fac73e0608770843ba66a24fcd093b73080f"


def _contract(tmp_path: Path, items: list[dict[str, object]]) -> str:
    path = tmp_path / "OMN-9999.yaml"
    path.write_text(
        yaml.safe_dump({"ticket_id": "OMN-9999", "dod_evidence": items}),
        encoding="utf-8",
    )
    return str(path)


def _accepted_item() -> dict[str, object]:
    return {
        "id": "dod-tests",
        "description": "proves the criterion it claims",
        "binds_ac": ["AC1"],
        "ac_bindings": [
            {
                "label": "AC1",
                "criterion_hash": _HASH,
                "accepted_by": "an-approving-reviewer",
                "accepted_at": "2026-09-12T21:00:00Z",
            }
        ],
        "checks": [{"check_type": "command", "check_value": "true"}],
    }


def test_a_contract_recording_a_binding_is_not_rejected(tmp_path: Path) -> None:
    """THE POINT: the record that proves a criterion must not fail the contract."""
    path = _contract(tmp_path, [_accepted_item()])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    rejections = [
        result
        for result in results
        if "INVALID_DOD_EVIDENCE_ITEM" in (result.message or "")
    ]
    assert rejections == [], [result.message for result in rejections]


def test_a_draft_record_is_accepted_too(tmp_path: Path) -> None:
    """A draft is a record with a hash and no acceptance.

    Whether a draft SATISFIES the closer is a separate question, decided
    downstream. It must not fail the contract at read time — a proposal that
    could not be written down could never be reviewed.
    """
    draft = _accepted_item()
    draft["ac_bindings"] = [{"label": "AC1", "criterion_hash": _HASH}]
    path = _contract(tmp_path, [draft])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert not any(
        "INVALID_DOD_EVIDENCE_ITEM" in (result.message or "") for result in results
    )


def test_the_claim_still_reaches_the_result_alongside_the_record(
    tmp_path: Path,
) -> None:
    """The OMN-18056 surfacing is untouched by adding the record beside it."""
    path = _contract(tmp_path, [_accepted_item()])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert results
    assert all(result.binds_ac == ("AC1",) for result in results)


def test_a_genuinely_unknown_field_is_still_rejected(tmp_path: Path) -> None:
    """The control. Widening the set by one must not open it.

    Without this, a change that accepted `ac_bindings` by loosening the gate
    rather than by naming the field would pass every test above.
    """
    item = _accepted_item()
    item["not_a_real_field"] = "x"
    path = _contract(tmp_path, [item])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert any(
        "INVALID_DOD_EVIDENCE_ITEM" in (result.message or "")
        and "not_a_real_field" in (result.message or "")
        for result in results
    )
