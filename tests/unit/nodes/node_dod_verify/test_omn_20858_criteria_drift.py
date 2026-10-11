# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20858 -- dod_verify refuses evidence bound to criteria the ticket no longer carries.

Runs the real ``HandlerDodVerify`` and the real ``EvidenceCollector`` over a
contract whose bindings are pinned to the hash of the criterion text they were
accepted against, with a recorded ticket body served through the injected
reader (no Linear call). Only the command runner and the live-PR read are
stubbed, as in the other verifier suites.

* AC1 -- an edited, deleted, added or duplicate-label criterion refuses with
  ``CRITERIA_DRIFT`` naming the criterion.
* AC2 -- a PASS bound to a changed criterion no longer counts.
* AC3 -- an unreadable ticket, and an edit made while the checks ran, refuse.
* AC4 -- an unchanged ticket verifies as before, OMN-19267's recorded criteria
  as one fixture.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    criteria_revision,
)
from omnimarket.occ_contract_pin import criterion_hash
from tests.test_dod_verify_acceptance_basis import _contract, _run
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import (
    RecordedTicketReader,
    criteria_body,
)
from tests.unit.nodes.node_dod_verify.test_receipt_bound_evidence_gate import (
    _CONTRACT_PATH,
    _RECEIPT_DIR,
    _TICKET,
    _make_gate,
    _receipt_bound_contract,
    _receipt_bound_receipt,
)

pytestmark = pytest.mark.unit

_FALSIFIERS = {
    "AC1": "uv run pytest tests/test_a.py -q -k a_case",
    "AC2": "uv run pytest tests/test_b.py -q",
}


def _statements() -> dict[str, str]:
    """The criterion text ``_contract`` transcribes for each label."""
    return {
        label: f"{label}: does a thing -- falsifier: {text}"
        for label, text in _FALSIFIERS.items()
    }


def _pinned_contract(
    *, extra_records: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A contract whose every binding is pinned to the real hash of its criterion."""
    contract = _contract(falsifiers=_FALSIFIERS)
    statements = _statements()
    item = contract["dod_evidence"][0]
    for record in item["ac_bindings"]:
        record["criterion_hash"] = criterion_hash(statements[record["label"]])
    item["ac_bindings"].extend(extra_records or [])
    return contract


def _verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[str | None],
    *,
    contract: dict[str, Any] | None = None,
) -> tuple[ModelDodVerifyState, RecordedTicketReader]:
    reader = RecordedTicketReader(bodies)
    state = _run(
        tmp_path,
        monkeypatch,
        contract if contract is not None else _pinned_contract(),
        ticket_reader=reader,
    )
    return state, reader


def _unchanged() -> str:
    return criteria_body(_statements())


def _drift_kinds(state: ModelDodVerifyState) -> dict[str, EnumCriteriaDriftKind]:
    return {drift.label: drift.kind for drift in state.criteria_drift}


def test_criteria_unchanged_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, reader = _verify(tmp_path, monkeypatch, [_unchanged()])
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.error_message is None
    assert state.criteria_drift == ()
    assert state.criteria_amendment is None
    assert state.criteria_revision == criteria_revision(_unchanged())
    # The ticket is read before the checks and again after them.
    assert reader.reads == 2


def test_criteria_unchanged_verifies_when_only_the_wrapping_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change-control hash collapses whitespace: a re-flowed paragraph is no rewrite."""
    statements = _statements()
    reflowed = criteria_body(
        {label: text.replace(" ", "   ") for label, text in statements.items()}
    )
    state, _ = _verify(tmp_path, monkeypatch, [reflowed])
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.criteria_drift == ()


def test_criteria_unchanged_verifies_omn_19267_recorded_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-19267's criteria as the repository recorded them, served as the ticket.

    The contract and the ticket body are the same recorded text; the verdict is
    the one the verifier gave before the criteria check existed.
    """
    recorded = yaml.safe_load(
        (
            Path(__file__).resolve().parents[4] / "contracts" / "OMN-19267.yaml"
        ).read_text(encoding="utf-8")
    )
    # `_run` verifies the contract it writes as OMN-20999; the criteria are the
    # recorded ones.
    recorded["ticket_id"] = "OMN-20999"
    # AC4 and AC5 are recorded without their label in front; the ticket line
    # carries it.
    criteria = {
        criterion["id"]: (
            criterion["statement"]
            if criterion["statement"].startswith(criterion["id"])
            else f"{criterion['id']}: {criterion['statement']}"
        )
        for requirement in recorded["requirements"]
        for criterion in requirement["acceptance"]
    }
    assert sorted(criteria) == ["AC1", "AC2", "AC4", "AC5", "AC6"]
    body = criteria_body(criteria)

    state, _ = _verify(tmp_path, monkeypatch, [body], contract=recorded)

    def _before_the_check_existed(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector,
        "_begin_criteria_check",
        _before_the_check_existed,
    )
    before_the_check_existed = _run(tmp_path / "control", monkeypatch, recorded)

    assert state.criteria_drift == ()
    assert state.criteria_revision == criteria_revision(body)
    assert state.status is before_the_check_existed.status
    assert state.error_message == before_the_check_existed.error_message
    assert state.verified_count == before_the_check_existed.verified_count
    assert state.total_checks == before_the_check_existed.total_checks
    assert before_the_check_existed.criteria_revision is None


def test_criteria_drift_edited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    state, _ = _verify(tmp_path, monkeypatch, [criteria_body(edited)])
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC1 edited" in state.error_message
    assert "AC2" not in " ".join(d.summary() for d in state.criteria_drift)
    assert _drift_kinds(state) == {"AC1": EnumCriteriaDriftKind.EDITED}
    [drift] = state.criteria_drift
    assert drift.pinned_hash == criterion_hash(_statements()["AC1"])
    assert drift.live_hash == criterion_hash(edited["AC1"])
    assert drift.item_ids == ("dod-OmniNode-ai-omnimarket-pr-3103",)


def test_criteria_drift_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = {"AC1": _statements()["AC1"]}
    state, _ = _verify(tmp_path, monkeypatch, [criteria_body(kept)])
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC2 deleted" in state.error_message
    assert _drift_kinds(state) == {"AC2": EnumCriteriaDriftKind.DELETED}


def test_criteria_drift_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    added = {**_statements(), "AC3": "AC3: a criterion nobody bound"}
    state, _ = _verify(tmp_path, monkeypatch, [criteria_body(added)])
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC3 added" in state.error_message
    assert _drift_kinds(state) == {"AC3": EnumCriteriaDriftKind.ADDED}


def test_criteria_drift_duplicate_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _unchanged() + "- AC2: a second criterion that also calls itself AC2\n"
    state, _ = _verify(tmp_path, monkeypatch, [body])
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "AC2 duplicate_label" in state.error_message
    assert _drift_kinds(state) == {"AC2": EnumCriteriaDriftKind.DUPLICATE_LABEL}


def test_an_annotation_below_the_criteria_block_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _unchanged() + "\n## Notes\n- AC2 MET 2026-10-10, see the PR\n"
    state, _ = _verify(tmp_path, monkeypatch, [body])
    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_criteria_drift_names_every_changed_criterion_in_one_amendment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four kinds at once: one refusal, one amendment record for the contract."""
    statements = _statements()
    body = criteria_body(
        {
            "AC1": "AC1: reworded",
            "AC3": "AC3: new",
            "AC4": "AC4: also new",
            "AC4b": "AC4: and again, differently",
        }
    )
    state, _ = _verify(tmp_path, monkeypatch, [body])
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert _drift_kinds(state) == {
        "AC1": EnumCriteriaDriftKind.EDITED,
        "AC2": EnumCriteriaDriftKind.DELETED,
        "AC3": EnumCriteriaDriftKind.ADDED,
        "AC4": EnumCriteriaDriftKind.DUPLICATE_LABEL,
    }
    amendment = state.criteria_amendment
    assert amendment is not None
    assert amendment.ticket_id == "OMN-20999"
    assert amendment.criteria_revision == criteria_revision(body)
    assert [e.label for e in amendment.rebind] == ["AC1"]
    assert amendment.rebind[0].from_hash == criterion_hash(statements["AC1"])
    assert amendment.rebind[0].to_hash == criterion_hash("AC1: reworded")
    assert [e.label for e in amendment.retire] == ["AC2"]
    assert [e.label for e in amendment.add] == ["AC3"]
    assert [e.label for e in amendment.disambiguate] == ["AC4"]
    for label in ("AC1", "AC2", "AC3", "AC4"):
        assert label in (state.error_message or "")


def test_changed_binding_invalidates_prior_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, _ = _verify(tmp_path / "control", monkeypatch, [_unchanged()])
    passes = {c.evidence_id: c.status for c in control.checks}
    assert passes["ac-falsifier-ac1"] is EnumEvidenceCheckStatus.VERIFIED
    assert passes["ac-falsifier-ac2"] is EnumEvidenceCheckStatus.VERIFIED

    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    state, _ = _verify(tmp_path / "edited", monkeypatch, [criteria_body(edited)])
    after = {c.evidence_id: c for c in state.checks}
    # The check ran and exited 0, and the pass no longer counts.
    assert after["ac-falsifier-ac1"].status is EnumEvidenceCheckStatus.SKIPPED
    assert (after["ac-falsifier-ac1"].message or "").startswith("CRITERIA_DRIFT")
    # The criterion that did not move keeps its pass.
    assert after["ac-falsifier-ac2"].status is EnumEvidenceCheckStatus.VERIFIED
    # The PR item binds both labels, so its pass rests on AC1 as well and stops
    # counting too: fewer passes count than before, never more.
    assert state.verified_count < control.verified_count
    assert state.behavior_proving_count < control.behavior_proving_count
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_changed_binding_counts_again_once_a_second_lane_re_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited_text = "AC1: does a different thing -- falsifier: x"
    reaccept = {
        "label": "AC1",
        "criterion_hash": criterion_hash(edited_text),
        "proposed_by": "occ-autobind",
        "accepted_by": "second-lane",
        "accepted_at": "2026-10-10T12:00:00Z",
    }
    body = criteria_body({**_statements(), "AC1": edited_text})
    state, _ = _verify(
        tmp_path,
        monkeypatch,
        [body],
        contract=_pinned_contract(extra_records=[reaccept]),
    )
    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_changed_binding_stays_unproven_when_its_author_re_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited_text = "AC1: does a different thing -- falsifier: x"
    self_accepted = {
        "label": "AC1",
        "criterion_hash": criterion_hash(edited_text),
        "proposed_by": "author-lane",
        "accepted_by": "author-lane",
        "accepted_at": "2026-10-10T12:00:00Z",
    }
    body = criteria_body({**_statements(), "AC1": edited_text})
    state, _ = _verify(
        tmp_path,
        monkeypatch,
        [body],
        contract=_pinned_contract(extra_records=[self_accepted]),
    )
    assert _drift_kinds(state) == {"AC1": EnumCriteriaDriftKind.EDITED}
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_ticket_unavailable_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, reader = _verify(tmp_path, monkeypatch, [None])
    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE
    assert state.error_message is not None
    assert state.error_message.startswith("VERIFICATION_UNRESOLVED: ticket_unavailable")
    assert state.criteria_revision is None
    assert state.criteria_drift == ()
    assert reader.reads == 2


def test_ticket_unavailable_refuses_when_only_the_second_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _ = _verify(tmp_path, monkeypatch, [_unchanged(), None])
    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE


def test_ticket_unavailable_refuses_a_contract_that_would_otherwise_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the same contract verifies when the ticket reads."""
    readable, _ = _verify(tmp_path / "readable", monkeypatch, [_unchanged()])
    unreadable, _ = _verify(tmp_path / "unreadable", monkeypatch, [None])
    assert readable.status is EnumDodVerifyStatus.VERIFIED
    assert unreadable.status is not EnumDodVerifyStatus.VERIFIED


def test_mid_verification_edit_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket matches the contract when the run starts and not when it ends."""
    edited = {**_statements(), "AC2": "AC2: edited while the checks ran"}
    state, reader = _verify(
        tmp_path, monkeypatch, [_unchanged(), criteria_body(edited)]
    )
    assert reader.reads == 2
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert "changed while this verification ran" in state.error_message
    assert state.criteria_revision == criteria_revision(_unchanged())
    # Checks that ran against a moving target do not count.
    assert state.verified_count == 0
    assert all(c.status is not EnumEvidenceCheckStatus.VERIFIED for c in state.checks)


def test_a_body_edit_that_leaves_the_criteria_alone_is_not_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after = "A new paragraph of context.\n" + _unchanged()
    state, _ = _verify(tmp_path, monkeypatch, [_unchanged(), after])
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_a_contract_that_records_no_criteria_reads_no_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(falsifiers={})
    state, reader = _verify(tmp_path, monkeypatch, [None], contract=contract)
    assert reader.reads == 0
    assert state.criteria_revision is None
    assert state.status is not EnumDodVerifyStatus.UNRESOLVED


def test_the_criteria_hash_is_the_change_control_definition() -> None:
    """sha256 over the whitespace-collapsed text; nothing else is normalised."""
    assert criterion_hash("a  b\n  c ") == hashlib.sha256(b"a b c").hexdigest()
    assert criterion_hash("A b c") != criterion_hash("a b c")
    assert criterion_hash("a b c.") != criterion_hash("a b c")


def test_the_revision_moves_with_a_criterion_and_not_with_the_prose() -> None:
    body = _unchanged()
    assert criteria_revision(body) == criteria_revision("intro\n" + body + "\noutro\n")
    assert criteria_revision(body) != criteria_revision(
        body.replace("does a thing", "does another thing", 1)
    )


# -- the Done transition re-checks the verdict's revision ---------------------


def _gate_evaluate(verdict_revision: str | None, description: str) -> Any:
    gate = _make_gate(
        tracked=True,
        receipts=[_receipt_bound_receipt()],
        contract_on_main=_receipt_bound_contract(),
    )
    return gate.evaluate(
        ticket_id=_TICKET,
        contract=_receipt_bound_contract(),
        receipt_dir=_RECEIPT_DIR,
        contract_rel_path=_CONTRACT_PATH,
        ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        ticket_description=description,
        verdict_criteria_revision=verdict_revision,
    )


def _revision_check(result: Any) -> Any:
    [check] = [
        c
        for c in result.checks
        if c.check is EnumDurableEvidenceCheck.CRITERIA_REVISION_CURRENT
    ]
    return check


def test_done_transition_passes_when_the_verdict_revision_is_still_live() -> None:
    body = _unchanged()
    result = _gate_evaluate(criteria_revision(body), body)
    assert result.status is EnumDurableEvidenceStatus.PASS
    assert _revision_check(result).passed is True


def test_done_transition_refuses_a_verdict_whose_criteria_moved() -> None:
    verdict = criteria_revision(_unchanged())
    result = _gate_evaluate(verdict, _unchanged().replace("a thing", "a new thing", 1))
    assert result.status is EnumDurableEvidenceStatus.FAIL
    check = _revision_check(result)
    assert check.passed is False
    assert check.message.startswith("CRITERIA_DRIFT")


def test_done_transition_refuses_a_revision_it_cannot_re_check() -> None:
    result = _gate_evaluate(criteria_revision(_unchanged()), "")
    assert result.status is EnumDurableEvidenceStatus.FAIL
    assert _revision_check(result).passed is False


def test_done_transition_without_a_verdict_revision_is_unchanged() -> None:
    result = _gate_evaluate(None, _unchanged())
    assert result.status is EnumDurableEvidenceStatus.PASS
    assert all(
        c.check is not EnumDurableEvidenceCheck.CRITERIA_REVISION_CURRENT
        for c in result.checks
    )
