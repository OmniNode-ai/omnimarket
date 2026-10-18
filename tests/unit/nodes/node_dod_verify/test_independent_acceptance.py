# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21062: a binding is accepted by a second lane that reruns its check.

The ruling (2026-10-03T00:40:00Z): a binding is never accepted by its author; a
second lane reruns the bound check and accepts it. The gate lives in
``node_dod_verify`` (``ac_falsifier_checks.is_accepted_binding`` and the
falsifier items ``derive_falsifier_items`` mints for an accepted label). These
tests run the real ``HandlerDodVerify`` and the real ``EvidenceCollector`` and
stub only the command runner and the live-PR read, so each case is a verdict the
gate gave, not a unit of one helper.

Selectors, one per acceptance criterion of the ticket:

* ``-k self_accepted_is_proposal``: an acceptance by the binding's own author
  decides nothing; the binding stays a proposal and its check is never rerun.
* ``-k acceptance_without_rerun_refused``: an acceptance by another lane with no
  passing rerun of the bound check does not verify.
* ``-k independent_rerun_accepted``: an acceptance by another lane whose rerun
  of the bound check passed verifies.

The change-control leg of the same rule is
``onex_change_control/tests/test_omn_17427_self_accepted_binding.py``; this file
does not read that repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_dod_acceptance_basis import EnumDodAcceptanceBasis
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector
from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    is_accepted_binding,
    self_accepted_bindings,
)
from tests.test_dod_verify_acceptance_basis import _contract, _run

pytestmark = pytest.mark.unit

_FALSIFIER = "uv run pytest tests/test_a.py -q -k a_case"
_AUTHOR = "evid-B13-2a21"
_SECOND_LANE = "verify-2a21"
_PR_ITEM = "dod-OmniNode-ai-omnimarket-pr-3103"
_BEHAVIOR_PROOF = {
    "id": "dod-occ-diff-derived-behavior-proof-pr-3103",
    "description": "diff-derived",
    "source": "generated",
    "checks": [
        {
            "check_type": "test_passes",
            "check_value": "uv run pytest tests/test_a.py -q",
            "cwd": "${OMNI_HOME}/omnimarket",
        }
    ],
}


def _falsifier_status(state: ModelDodVerifyState) -> EnumEvidenceCheckStatus | None:
    for check in state.checks:
        if check.evidence_id == "ac-falsifier-ac1":
            return check.status
    return None


def _reaccepted(accepted_by: str) -> dict[str, Any]:
    """A later record in which ``accepted_by`` accepts the author's AC1 binding."""
    return {
        "id": "dod-reacceptance-ac1",
        "description": "second lane re-accepts the author's AC1 binding",
        "source": "generated",
        "checks": [{"check_type": "command", "check_value": "true"}],
        "binds_ac": ["AC1"],
        "ac_bindings": [
            {
                "label": "AC1",
                "criterion_hash": "a" * 64,
                "proposed_by": _AUTHOR,
                "accepted_by": accepted_by,
                "accepted_at": "2026-09-30T02:00:00Z",
            }
        ],
    }


def test_self_accepted_is_proposal_by_the_acceptance_rule() -> None:
    own = {"proposed_by": _AUTHOR, "accepted_by": _AUTHOR}
    own_on_host = {"proposed_by": _AUTHOR, "accepted_by": f"{_AUTHOR}@mac"}
    own_by_lane_token = {"proposed_by": _AUTHOR, "accepted_by": f"lane={_AUTHOR}"}
    other = {"proposed_by": _AUTHOR, "accepted_by": _SECOND_LANE}
    unaccepted = {"proposed_by": _AUTHOR}
    assert not is_accepted_binding(own)
    assert not is_accepted_binding(own_on_host)
    assert not is_accepted_binding(own_by_lane_token)
    assert not is_accepted_binding(unaccepted)
    assert is_accepted_binding(other)


def test_self_accepted_is_proposal_and_is_reported_by_name() -> None:
    contract = _contract(
        falsifiers={"AC1": _FALSIFIER}, proposed_by=_AUTHOR, accepted_by=_AUTHOR
    )
    assert self_accepted_bindings(contract["dod_evidence"]) == (
        f"{_PR_ITEM}:AC1 accepted_by={_AUTHOR}",
    )


def test_self_accepted_is_proposal_and_the_verdict_does_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing behavior proof beside it cannot stand in for the second lane."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER},
            proposed_by=_AUTHOR,
            accepted_by=_AUTHOR,
            extra_items=[_BEHAVIOR_PROOF],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_BINDING_SELF_ACCEPTED")
    assert state.acceptance_self_accepted_bindings == (
        f"{_PR_ITEM}:AC1 accepted_by={_AUTHOR}",
    )
    # A proposal is not rerun: its falsifier never becomes an evidence item.
    assert _falsifier_status(state) is None


def test_self_accepted_is_proposal_when_the_binding_carries_no_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(falsifiers={"AC1": _FALSIFIER}, proposed_by=_AUTHOR)
    del contract["dod_evidence"][0]["ac_bindings"][0]["accepted_by"]
    state = _run(tmp_path, monkeypatch, contract)
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_self_accepted_bindings == (
        f"{_PR_ITEM}:AC1 accepted_by=<none>",
    )
    assert _falsifier_status(state) is None


def test_self_accepted_is_proposal_even_beside_another_authors_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second lane's acceptance of a different author's record is no rerun of this one."""
    bystander = _reaccepted(_SECOND_LANE)
    bystander["ac_bindings"][0]["proposed_by"] = "occ-autobind"
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER},
            proposed_by=_AUTHOR,
            accepted_by=_AUTHOR,
            extra_items=[bystander],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_self_accepted_bindings == (
        f"{_PR_ITEM}:AC1 accepted_by={_AUTHOR}",
    )


def test_acceptance_without_rerun_refused_when_the_check_is_not_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another lane accepted the binding, but its check produced no passing rerun."""
    original = evidence_collector.EvidenceCollector._check_evidence_item

    def _never_reran(self: Any, item: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        result = original(self, item, *args, **kwargs)
        if str(item.get("id", "")).startswith("ac-falsifier-"):
            return result.model_copy(
                update={
                    "status": EnumEvidenceCheckStatus.SKIPPED,
                    "message": "not rerun",
                }
            )
        return result

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector, "_check_evidence_item", _never_reran
    )
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER},
            proposed_by=_AUTHOR,
            accepted_by=_SECOND_LANE,
        ),
    )
    assert state.acceptance_self_accepted_bindings == ()
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_FALSIFIER_NOT_VERIFIED")


def test_acceptance_without_rerun_refused_when_the_rerun_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER},
            proposed_by=_AUTHOR,
            accepted_by=_SECOND_LANE,
        ),
        failing=frozenset({_FALSIFIER}),
    )
    assert state.acceptance_self_accepted_bindings == ()
    assert _falsifier_status(state) is EnumEvidenceCheckStatus.FAILED
    assert state.status is EnumDodVerifyStatus.FAILED


def test_acceptance_without_rerun_refused_when_there_is_no_runnable_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acceptance with nothing to rerun cannot stand for a rerun."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": "select count(*) from t on the dev lane"},
            proposed_by=_AUTHOR,
            accepted_by=_SECOND_LANE,
        ),
    )
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIERS_UNRUNNABLE
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert _falsifier_status(state) is None


def test_acceptance_without_rerun_refused_when_the_only_evidence_is_pr_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(falsifiers={}, proposed_by=_AUTHOR, accepted_by=_SECOND_LANE),
    )
    assert state.acceptance_basis is EnumDodAcceptanceBasis.NO_ACCEPTANCE_CHECKS
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")


def test_independent_rerun_accepted_verifies_on_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER},
            proposed_by=_AUTHOR,
            accepted_by=_SECOND_LANE,
        ),
    )
    assert state.acceptance_self_accepted_bindings == ()
    assert _falsifier_status(state) is EnumEvidenceCheckStatus.VERIFIED
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIER_CHECKS
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_independent_rerun_accepted_when_a_second_lane_reaccepts_the_authors_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged item cannot be edited; the re-acceptance is an appended record."""
    contract = _contract(
        falsifiers={"AC1": _FALSIFIER},
        proposed_by=_AUTHOR,
        accepted_by=_AUTHOR,
        extra_items=[_reaccepted(_SECOND_LANE)],
    )
    assert self_accepted_bindings(contract["dod_evidence"]) == ()
    state = _run(tmp_path, monkeypatch, contract)
    assert state.acceptance_self_accepted_bindings == ()
    assert _falsifier_status(state) is EnumEvidenceCheckStatus.VERIFIED
    assert state.status is EnumDodVerifyStatus.VERIFIED
