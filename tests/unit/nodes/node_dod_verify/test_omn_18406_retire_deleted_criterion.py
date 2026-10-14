# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18406 -- a legal way to retire evidence bound to a criterion the ticket deleted.

A merged ``dod_evidence`` entry is immutable (the OCC append-only gate), and since
OMN-20858 dod_verify refuses evidence bound to a criterion the live ticket no longer
carries. Together they deadlocked: nothing an author could append took the deleted
criterion's binding out of the drift check. The one record both gates read is the
existing ``supersedes_ac_binding`` entry, appended on a new item: OCC's binding gate
already honours it, and dod_verify now honours it for a deleted criterion, including
over a binding a second lane accepted.

* AC1 -- evidence for a deleted criterion plus the retirement passes dod_verify, and
  the appended retirement passes the append-only gate.
* AC2 -- the same contract without the retirement is still refused ``CRITERIA_DRIFT``.
* A retirement of an accepted binding whose criterion the ticket still carries refuses.
* A deleted criterion with one claim left unretired still refuses, naming that item.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validation.validator_occ_append_only import evaluate_append_only

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.ac_binding_retirements import (
    resolve_retirements,
)
from omnimarket.occ_contract_pin import criterion_hash
from tests.test_dod_verify_acceptance_basis import _contract, _run
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import (
    RecordedTicketReader,
    criteria_body,
)

pytestmark = pytest.mark.unit

_FALSIFIERS = {
    "AC1": "uv run pytest tests/test_a.py -q -k a_case",
    "AC2": "uv run pytest tests/test_b.py -q",
}
_PR_ITEM = "dod-OmniNode-ai-omnimarket-pr-3103"
_AC2_ONLY = "dod-ac2-only"
_AC2_ONLY_CHECK = "ac2-only-check"


def _statement(label: str) -> str:
    return f"{label}: does a thing -- falsifier: {_FALSIFIERS[label]}"


def _accepted_record(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "criterion_hash": criterion_hash(_statement(label)),
        "proposed_by": "occ-autobind",
        "accepted_by": "author-uuid",
        "accepted_at": "2026-09-30T01:00:00Z",
    }


def _base_contract() -> dict[str, Any]:
    """The merged contract: AC1 and AC2 pinned and accepted, AC2 also on its own item."""
    contract = _contract(falsifiers=_FALSIFIERS)
    contract["dod_evidence"][0]["ac_bindings"] = [
        _accepted_record(label) for label in _FALSIFIERS
    ]
    contract["dod_evidence"].append(
        {
            "id": _AC2_ONLY,
            "description": "AC2 evidence, written when the ticket still had AC2.",
            "checks": [{"check_type": "command", "check_value": _AC2_ONLY_CHECK}],
            "binds_ac": ["AC2"],
            "ac_bindings": [_accepted_record("AC2")],
        }
    )
    return contract


def _retirement(item: str, label: str = "AC2") -> dict[str, Any]:
    return {
        "item": item,
        "label": label,
        "reason_kind": "no_longer_applicable",
        "reason": f"The ticket dropped {label}; this evidence settles nothing now.",
        "retired_by": "omn18406-2aff",
        "retired_at": "2026-10-13T14:00:00Z",
    }


def _carrier(*entries: dict[str, Any], item_id: str = "dod-retire-ac2") -> dict:
    return {
        "id": item_id,
        "description": "Retires the bindings of the criterion the ticket dropped.",
        "checks": [{"check_type": "command", "check_value": "true"}],
        "supersedes_ac_binding": list(entries),
    }


def _after_deletion(*carriers: dict[str, Any]) -> dict[str, Any]:
    """The amended contract: AC2 out of the editable requirements, carriers appended."""
    contract = _base_contract()
    acceptance = contract["requirements"][0]["acceptance"]
    contract["requirements"][0]["acceptance"] = [
        criterion for criterion in acceptance if criterion["id"] != "AC2"
    ]
    contract["dod_evidence"].extend(carriers)
    return contract


def _verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: dict[str, Any],
    labels: tuple[str, ...] = ("AC1",),
    *,
    failing: frozenset[str] = frozenset(),
) -> ModelDodVerifyState:
    body = criteria_body({label: _statement(label) for label in labels})
    return _run(
        tmp_path,
        monkeypatch,
        contract,
        failing=failing,
        ticket_reader=RecordedTicketReader([body]),
    )


def _status(state: ModelDodVerifyState, evidence_id: str) -> EnumEvidenceCheckStatus:
    return next(c.status for c in state.checks if c.evidence_id == evidence_id)


def test_ac1_retired_deleted_criterion_passes_dod_verify_and_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _after_deletion(_carrier(_retirement(_PR_ITEM), _retirement(_AC2_ONLY)))
    state = _verify(tmp_path, monkeypatch, contract)
    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED, state.error_message
    assert state.error_message is None

    appended = evaluate_append_only(_base_contract(), contract)
    assert appended.violations == ()
    # Positive control: the gate does refuse withdrawing AC2 by editing the entry.
    edited = deepcopy(contract)
    edited["dod_evidence"][1]["binds_ac"] = []
    assert evaluate_append_only(_base_contract(), edited).violations


def test_ac2_deleted_criterion_without_retirement_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _verify(tmp_path, monkeypatch, _after_deletion())
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC2 deleted" in state.error_message
    [drift] = state.criteria_drift
    assert drift.kind is EnumCriteriaDriftKind.DELETED
    assert drift.item_ids == (_PR_ITEM, _AC2_ONLY)


def test_a_claim_left_unretired_still_refuses_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _verify(
        tmp_path, monkeypatch, _after_deletion(_carrier(_retirement(_PR_ITEM)))
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    [drift] = state.criteria_drift
    assert drift.kind is EnumCriteriaDriftKind.DELETED
    assert drift.item_ids == (_AC2_ONLY,)


def test_a_criterion_still_declared_by_the_contract_is_not_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requirements block is editable, so its declaration of AC2 must go too."""
    contract = _base_contract()
    contract["dod_evidence"].append(
        _carrier(_retirement(_PR_ITEM), _retirement(_AC2_ONLY))
    )
    state = _verify(tmp_path, monkeypatch, contract)
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert [d.kind for d in state.criteria_drift] == [EnumCriteriaDriftKind.DELETED]


def test_retiring_an_accepted_binding_of_a_live_criterion_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _base_contract()
    contract["dod_evidence"].append(_carrier(_retirement(_AC2_ONLY)))
    state = _verify(tmp_path, monkeypatch, contract, ("AC1", "AC2"))
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC2 retired while the ticket still carries it" in state.error_message
    [drift] = state.criteria_drift
    assert drift.kind is EnumCriteriaDriftKind.RETIRED_LIVE
    assert drift.item_ids == (_AC2_ONLY,)


def test_the_same_contract_verifies_without_that_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the refusal above: only the retirement moved the verdict."""
    state = _verify(tmp_path, monkeypatch, _base_contract(), ("AC1", "AC2"))
    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED, state.error_message


def test_evidence_bound_only_to_the_retired_criterion_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its check settles no criterion the ticket has, so its red does not count."""
    contract = _after_deletion(_carrier(_retirement(_PR_ITEM), _retirement(_AC2_ONLY)))
    state = _verify(
        tmp_path, monkeypatch, contract, failing=frozenset({_AC2_ONLY_CHECK})
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED, state.error_message
    assert _status(state, _AC2_ONLY) is EnumEvidenceCheckStatus.SKIPPED
    retired = next(c for c in state.checks if c.evidence_id == _AC2_ONLY)
    assert retired.message is not None
    assert retired.message.startswith("CRITERION_RETIRED")
    # The item that still binds a live criterion keeps counting.
    assert _status(state, _PR_ITEM) is EnumEvidenceCheckStatus.VERIFIED


def test_a_typed_retirement_appended_after_an_untyped_one_takes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged legacy entry (no reason_kind) cannot be fixed in place, so it must
    not own the pair against the typed entry appended to replace it."""
    legacy = {"item": _AC2_ONLY, "label": "AC2", "reason": "dropped from the ticket"}
    contract = _after_deletion(
        _carrier(legacy, item_id="dod-legacy-retirement"),
        _carrier(_retirement(_PR_ITEM), _retirement(_AC2_ONLY)),
    )
    resolution = resolve_retirements(contract["dod_evidence"])
    assert any("RETIREMENT_UNTYPED" in refusal for refusal in resolution.refused)
    assert {(r.item, r.label) for r in resolution.retired_if_absent} == {
        (_PR_ITEM, "AC2"),
        (_AC2_ONLY, "AC2"),
    }
    state = _verify(tmp_path, monkeypatch, contract)
    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED, state.error_message


def test_an_accepted_retirement_still_reads_refused_without_the_ticket() -> None:
    """Without the live ticket nothing says the criterion is gone, so the claim stays."""
    contract = _after_deletion(_carrier(_retirement(_PR_ITEM)))
    before = deepcopy(contract)
    resolution = resolve_retirements(contract["dod_evidence"])
    assert resolution.pairs == frozenset()
    assert "RETIREMENT_BINDING_ACCEPTED" in resolution.refused[0]
    assert [r.item for r in resolution.retired_if_absent] == [_PR_ITEM]
    assert contract == before
