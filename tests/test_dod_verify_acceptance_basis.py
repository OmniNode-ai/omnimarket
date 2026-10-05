# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20153 AC2 and AC3 -- the DoD verdict requires the author's falsifiers.

Runs the real ``HandlerDodVerify`` and the real ``EvidenceCollector`` over a
contract file in the shape ``occ-autobind`` mints, with only the two effects
that leave the machine stubbed: the command runner and the live-PR read.

* AC2: a falsifier item that fails, or whose selector runs zero tests, makes
  the verdict FAILED; one that is merely not verified stops it reading VERIFIED.
* AC3: a contract that declares no falsifier and holds only PR-exists, grep or
  readback evidence reports NO_ACCEPTANCE_CHECKS and is not VERIFIED.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_dod_acceptance_basis import EnumDodAcceptanceBasis
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector
from tests.unit.nodes.node_dod_verify.omn_19428_occ_tree import occ_tree

pytestmark = pytest.mark.unit

_FALSIFIER_A = "uv run pytest tests/test_a.py -q -k a_case"
_FALSIFIER_B = "uv run pytest tests/test_b.py -q"
_PR_ITEM = "dod-OmniNode-ai-omnimarket-pr-3103"


def _contract(
    *,
    falsifiers: dict[str, str],
    extra_items: list[dict[str, Any]] | None = None,
    proposed_by: str = "occ-autobind",
    accepted_by: str = "author-uuid",
) -> dict[str, Any]:
    accepted = [
        {
            "label": label,
            "criterion_hash": "a" * 64,
            "proposed_by": proposed_by,
            "accepted_by": accepted_by,
            "accepted_at": "2026-09-30T01:00:00Z",
        }
        for label in falsifiers
    ]
    pr_item: dict[str, Any] = {
        "id": _PR_ITEM,
        "description": "PR #3103 evidence.",
        "source": "generated",
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    "gh api repos/OmniNode-ai/omnimarket/contents/tests/test_a.py"
                    "?ref=c8879d914003ebdf2e43ba16908559536bc6dbbb --jq '.content' "
                    "| base64 -d | grep -c 'def test_a_case'"
                ),
            }
        ],
    }
    if falsifiers:
        pr_item["binds_ac"] = list(falsifiers)
        pr_item["ac_bindings"] = accepted
    contract: dict[str, Any] = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-20999",
        "title": "acceptance basis",
        "dod_evidence": [pr_item, *(extra_items or [])],
    }
    if falsifiers:
        contract["requirements"] = [
            {
                "id": "req-transcribed-acceptance-criteria",
                "statement": "transcribed",
                "acceptance": [
                    {
                        "id": label,
                        "statement": f"{label}: does a thing -- falsifier: {text}",
                    }
                    for label, text in falsifiers.items()
                ],
            }
        ]
    return contract


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: dict[str, Any],
    *,
    failing: frozenset[str] = frozenset(),
    behavior_extra: bool = False,
) -> ModelDodVerifyState:
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket" / "tests").mkdir(parents=True)
    (omni_home / "omnimarket" / "tests" / "test_a.py").write_text("")
    (omni_home / "omnimarket" / "tests" / "test_b.py").write_text("")
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    executed: list[str] = []

    def _fake_run(
        _self: Any, check: dict[str, Any], *_a: Any, **_k: Any
    ) -> tuple[bool, str]:
        value = str(check.get("check_value") or check.get("command"))
        executed.append(value)
        if value in failing:
            return False, "no tests ran" if "no-tests" in value else "1 failed"
        return True, "ok"

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector, "_run_command_check", _fake_run
    )
    monkeypatch.setattr(
        evidence_collector.EvidenceCollector,
        "_verify_live_pr",
        lambda _self, repo, pr_number: (
            EnumEvidenceCheckStatus.VERIFIED,
            f"{repo}#{pr_number}: MERGED (stub)",
            None,
        ),
    )
    monkeypatch.setenv(evidence_collector._ALLOW_STALE_PRODUCT_CLONE_ENV, "1")

    # The collector reads every item field the core model does not own (here
    # ``ac_bindings``) from the model in the OCC tree the contract lives in.
    root = occ_tree(tmp_path)
    path = root / "contracts" / "OMN-20999.yaml"
    path.write_text(yaml.safe_dump(contract))
    command = ModelDodVerifyStartCommand(
        correlation_id=uuid.uuid4(),
        ticket_id="OMN-20999",
        contract_path=str(path),
        execution_audience="hosted",
        requested_at=datetime.now(tz=UTC),
    )
    state = HandlerDodVerify()._handle_typed(command)
    assert isinstance(state, ModelDodVerifyState)
    state_executed = executed
    assert state_executed is executed
    return state


def _by_id(state: ModelDodVerifyState) -> dict[str, EnumEvidenceCheckStatus]:
    return {check.evidence_id: check.status for check in state.checks}


def test_falsifier_becomes_an_executed_check_and_sets_the_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(falsifiers={"AC1": _FALSIFIER_A, "AC2": _FALSIFIER_B}),
    )
    statuses = _by_id(state)
    assert statuses["ac-falsifier-ac1"] is EnumEvidenceCheckStatus.VERIFIED
    assert statuses["ac-falsifier-ac2"] is EnumEvidenceCheckStatus.VERIFIED
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIER_CHECKS
    assert state.acceptance_declared_falsifier_count == 2
    assert state.acceptance_runnable_falsifier_count == 2
    assert state.behavior_proving_count >= 2
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_falsifier_item_failure_fails_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(falsifiers={"AC1": _FALSIFIER_A, "AC2": _FALSIFIER_B}),
        failing=frozenset({_FALSIFIER_B}),
    )
    statuses = _by_id(state)
    assert statuses["ac-falsifier-ac1"] is EnumEvidenceCheckStatus.VERIFIED
    assert statuses["ac-falsifier-ac2"] is EnumEvidenceCheckStatus.FAILED
    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.failed_count >= 1


def test_falsifier_naming_a_missing_test_file_fails_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OMN-19533 class: a falsifier naming a test that does not exist."""
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(falsifiers={"AC1": "uv run pytest tests/test_gone.py -q"}),
        failing=frozenset({"uv run pytest tests/test_gone.py -q"}),
    )
    assert _by_id(state)["ac-falsifier-ac1"] is EnumEvidenceCheckStatus.FAILED
    assert state.status is EnumDodVerifyStatus.FAILED


def test_an_unverified_falsifier_item_stops_the_verdict_reading_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain skip is non-blocking for ordinary items; not for a falsifier."""
    original = evidence_collector.EvidenceCollector._check_evidence_item

    def _skip_falsifiers(
        self: Any, item: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        result = original(self, item, *args, **kwargs)
        if str(item.get("id", "")).startswith("ac-falsifier-"):
            return result.model_copy(
                update={"status": EnumEvidenceCheckStatus.SKIPPED, "message": "skip"}
            )
        return result

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector, "_check_evidence_item", _skip_falsifiers
    )
    state = _run(tmp_path, monkeypatch, _contract(falsifiers={"AC1": _FALSIFIER_A}))
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_FALSIFIER_NOT_VERIFIED")


def test_no_falsifiers_reports_no_acceptance_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract whose only passing evidence is PR-exists reads no acceptance
    checks, not VERIFIED."""
    state = _run(tmp_path, monkeypatch, _contract(falsifiers={}))
    assert state.acceptance_basis is EnumDodAcceptanceBasis.NO_ACCEPTANCE_CHECKS
    assert state.behavior_proving_count == 0
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")


def test_unrunnable_falsifiers_are_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(falsifiers={"AC1": "select count(*) from t on the dev lane"}),
    )
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIERS_UNRUNNABLE
    assert state.acceptance_unrunnable_labels == ("AC1",)
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_no_falsifiers_but_a_behavior_proof_keeps_its_verdict_and_names_the_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = {
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
    state = _run(tmp_path, monkeypatch, _contract(falsifiers={}, extra_items=[proof]))
    assert state.acceptance_basis is EnumDodAcceptanceBasis.NO_ACCEPTANCE_CHECKS
    assert state.behavior_proving_count == 1
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_caller_supplied_results_carry_no_acceptance_basis() -> None:
    """Nothing was loaded, so nothing is claimed: None, never NO_ACCEPTANCE."""
    from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
        ModelEvidenceCheckResult,
    )

    command = ModelDodVerifyStartCommand(
        correlation_id=uuid.uuid4(),
        ticket_id="OMN-20999",
        contract_path="unused",
        execution_audience="hosted",
        requested_at=datetime.now(tz=UTC),
    )
    state = HandlerDodVerify()._handle_typed(
        command,
        [
            ModelEvidenceCheckResult(
                evidence_id="x",
                description="x",
                status=EnumEvidenceCheckStatus.VERIFIED,
            )
        ],
    )
    assert state.acceptance_basis is None


def test_self_accepted_binding_refuses_the_verdict_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-17427: even a behavior proof cannot accept its author's binding."""
    proof = {
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
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER_A},
            proposed_by="mac-occ-contracts",
            accepted_by="mac-occ-contracts",
            extra_items=[proof],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_BINDING_SELF_ACCEPTED")
    assert state.acceptance_self_accepted_bindings == (
        "dod-OmniNode-ai-omnimarket-pr-3103:AC1 accepted_by=mac-occ-contracts",
    )
    pr_check = next(check for check in state.checks if check.evidence_id == _PR_ITEM)
    assert pr_check.draft_binds_ac == ("AC1",)


def test_binding_accepted_by_another_lane_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER_A},
            proposed_by="mac-occ-contracts",
            accepted_by="verify-2a21",
        ),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIER_CHECKS
    assert state.acceptance_self_accepted_bindings == ()


def test_autobind_record_on_the_label_does_not_hide_a_self_accepted_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-17427: OMN-19405's shape -- a person-accepted autobind record shares the label."""
    autobind = {
        "id": "dod-OmniNode-ai-omnibase_core-pr-1754",
        "description": "autobind original",
        "source": "generated",
        "checks": [{"check_type": "command", "check_value": "true"}],
        "binds_ac": ["AC1"],
        "ac_bindings": [
            {
                "label": "AC1",
                "criterion_hash": "a" * 64,
                "proposed_by": "occ-autobind",
                "accepted_by": "7a850ce1-f95e-431f-b4e3-62f7449f04c0",
                "accepted_at": "2026-09-24T15:41:02Z",
            }
        ],
    }
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER_A},
            proposed_by="evid-B13-2a21",
            accepted_by="evid-B13-2a21",
            extra_items=[autobind],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_BINDING_SELF_ACCEPTED")
    assert state.acceptance_self_accepted_bindings == (
        "dod-OmniNode-ai-omnimarket-pr-3103:AC1 accepted_by=evid-B13-2a21",
    )


# OMN-19267: a supersession marker the contract aims at a DECLARED item that
# happens to share the derived falsifier's id (``ac-falsifier-<label>``) used to
# retire the verifier-derived item too, because supersession was keyed by id
# over the declared AND the derived items. The derived falsifier then never
# ran, and AC_FALSIFIER_NOT_VERIFIED held the verdict forever. A marker only
# ever retires a declared item; the derived falsifier always executes and its
# own result decides the criterion.
_COLLIDING_DECLARED = {
    "id": "ac-falsifier-ac1",
    "description": "a declared item that reuses the derived falsifier id",
    "source": "generated",
    "checks": [
        {
            "check_type": "test_passes",
            "check_value": "uv run pytest tests/test_b.py -q -k declared_copy",
            "cwd": "${OMNI_HOME}/omnimarket",
        }
    ],
}
_RETIRING_CARRIER = {
    "id": "dod-carrier-retires-declared-ac1",
    "description": "retires the declared copy, and only it",
    "source": "generated",
    "checks": [
        {
            "check_type": "test_passes",
            "check_value": "uv run pytest tests/test_b.py -q -k carrier",
            "cwd": "${OMNI_HOME}/omnimarket",
        }
    ],
    "evidence_artifact": "supersedes_dod_evidence:ac-falsifier-ac1",
}


def _collision_contract() -> dict[str, Any]:
    return _contract(
        falsifiers={"AC1": _FALSIFIER_A},
        extra_items=[dict(_COLLIDING_DECLARED), dict(_RETIRING_CARRIER)],
    )


def _derived_results(state: ModelDodVerifyState) -> list[Any]:
    return [
        check
        for check in state.checks
        if _FALSIFIER_A in check.description and "::" not in check.evidence_id
    ]


def test_id_collision_marker_aimed_at_a_declared_item_spares_the_derived_falsifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(tmp_path, monkeypatch, _collision_contract())
    derived = _derived_results(state)
    assert len(derived) == 1
    assert derived[0].status is EnumEvidenceCheckStatus.VERIFIED
    # The declared copy is still retired, preserved for audit.
    declared = [c for c in state.checks if c.evidence_id == "ac-falsifier-ac1"]
    assert [c.status for c in declared] == [EnumEvidenceCheckStatus.SUPERSEDED]
    # The derived id never shares a declared id, so no result is ambiguous.
    assert derived[0].evidence_id != "ac-falsifier-ac1"
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_basis is EnumDodAcceptanceBasis.FALSIFIER_CHECKS


def test_id_collision_derived_falsifier_failure_still_fails_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stricter, not looser: the derived falsifier runs and its failure counts."""
    state = _run(
        tmp_path,
        monkeypatch,
        _collision_contract(),
        failing=frozenset({_FALSIFIER_A}),
    )
    derived = _derived_results(state)
    assert [c.status for c in derived] == [EnumEvidenceCheckStatus.FAILED]
    assert state.status is EnumDodVerifyStatus.FAILED


def test_id_collision_declared_item_cannot_stand_in_for_the_derived_falsifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared item sharing the derived id does not prove the derived check."""
    original = evidence_collector.EvidenceCollector._check_evidence_item

    def _skip_derived(
        self: Any, item: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        result = original(self, item, *args, **kwargs)
        if item.get("source") == "generated" and _FALSIFIER_A in str(
            item.get("description", "")
        ):
            return result.model_copy(
                update={"status": EnumEvidenceCheckStatus.SKIPPED, "message": "skip"}
            )
        return result

    monkeypatch.setattr(
        evidence_collector.EvidenceCollector, "_check_evidence_item", _skip_derived
    )
    state = _run(
        tmp_path,
        monkeypatch,
        _contract(
            falsifiers={"AC1": _FALSIFIER_A},
            extra_items=[dict(_COLLIDING_DECLARED)],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("AC_FALSIFIER_NOT_VERIFIED")
