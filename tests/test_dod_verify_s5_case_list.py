# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 constructed cases through the real verifier and the difference classifier (OMN-20072).

``test_dod_verify_occ_difference.py`` feeds the classifier hand-written verdict
dicts. This file covers the other half of the chain: each constructed pilot
case runs through the real ``HandlerDodVerify`` over a repo-shaped contract, the
state is serialised as the CLI prints it (``model_dump_json(indent=2)``),
written as the ``head-<ticket>.json`` artifact the workflow step leaves, and
read back by ``load_new_verdict`` and ``classify``. A renamed verdict field or
a reworded refusal therefore moves a reason code here, where a hand-written
dict would not notice.

Only the two effects that leave the machine are stubbed: the command runner and
the live-PR read. The base-branch control line is the text the workflow's shell
step writes (``passed: ...`` or ``refused``); the shell step itself is not run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_dod_acceptance_basis import EnumDodAcceptanceBasis
from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)
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
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelOccVerdict,
    ModelOccVerdictDifferenceResult,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    classify,
    load_new_verdict,
)
from tests.unit.nodes.node_dod_verify.omn_19428_occ_tree import occ_tree

pytestmark = pytest.mark.unit

_TICKET = "OMN-20999"
_TEST_A = "uv run pytest tests/test_a.py -q"
_TEST_B = "uv run pytest tests/test_b.py -q"
_INTEGRATION = "uv run pytest tests/test_integration.py -q"
_MERGE_PROBE = (
    "gh pr view 3277 --repo OmniNode-ai/omnimarket --json state --jq '.state' "
    "| grep -q MERGED"
)
_CONTROL_PASSED = "passed: every bound check failed"
_CONTROL_REFUSED = "refused"


def _item(
    item_id: str,
    command: str,
    binds_ac: list[str],
    *,
    check_type: str = "test_passes",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "description": item_id,
        "source": "manual",
        "checks": [
            {
                "check_type": check_type,
                "check_value": command,
                "cwd": "${OMNI_HOME}/omnimarket",
            }
        ],
    }
    if binds_ac:
        item["binds_ac"] = binds_ac
    return item


def _contract(criteria: list[str], items: list[dict[str, Any]]) -> str:
    return yaml.safe_dump(
        {
            "schema_version": "1.0.0",
            "ticket_id": _TICKET,
            "title": "constructed S5 case",
            "requirements": [
                {
                    "id": "req-1",
                    "statement": "constructed",
                    "acceptance": [
                        {"id": label, "statement": f"{label}: constructed criterion"}
                        for label in criteria
                    ],
                }
            ],
            "dod_evidence": items,
        }
    )


def _verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_text: str,
    *,
    failing: frozenset[str] = frozenset(),
) -> ModelDodVerifyState:
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket" / "tests").mkdir(parents=True)
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    def _fake_run(
        _self: Any, check: dict[str, Any], *_args: Any, **_kwargs: Any
    ) -> tuple[bool, str]:
        value = str(check.get("check_value") or check.get("command"))
        return (False, "1 failed") if value in failing else (True, "ok")

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

    path = occ_tree(tmp_path) / "contracts" / f"{_TICKET}.yaml"
    path.write_text(contract_text, encoding="utf-8")
    state = HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id=_TICKET,
            contract_path=str(path),
            execution_audience="hosted",
            requested_at=datetime.now(tz=UTC),
        )
    )
    assert isinstance(state, ModelDodVerifyState)
    return state


def _difference(
    tmp_path: Path,
    state: ModelDodVerifyState,
    control: str,
    *,
    negative_control: bool = False,
) -> ModelOccVerdictDifferenceResult:
    """Write the head and control artifacts, read them back, compare with an OCC admit."""
    dod_dir = tmp_path / f"dod-{uuid.uuid4().hex}"
    dod_dir.mkdir()
    (dod_dir / f"head-{_TICKET}.json").write_text(
        state.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (dod_dir / f"base-{_TICKET}.control.txt").write_text(
        control + "\n", encoding="utf-8"
    )
    return classify(
        ModelOccVerdict(admitted=True, conclusion="success"),
        load_new_verdict(dod_dir, [_TICKET]),
        negative_control,
    )


def _assert_labelled_control_is_refused(
    tmp_path: Path, state: ModelDodVerifyState, control: str
) -> None:
    """A PR labelled as a negative control must be refused, whatever OCC said."""
    labelled = _difference(tmp_path, state, control, negative_control=True)
    assert labelled.new_admitted is False
    assert labelled.outcome == "negative_control_refused"
    assert labelled.passed is True


def test_real_fix_is_admitted_and_agrees_with_occ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a bound test that passes at the head and failed at the base."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(["AC1"], [_item("dod-1", _TEST_A, ["AC1"])]),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert [check.binds_ac for check in state.checks] == [("AC1",)]

    result = _difference(tmp_path, state, _CONTROL_PASSED)
    assert result.new_admitted is True
    assert result.outcome == "agree"
    assert result.passed is True


def test_readback_only_contract_reads_readback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weak contract: the only bound evidence reads repository state."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1"],
            [_item("dod-1", "git rev-parse HEAD", ["AC1"], check_type="command")],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")

    result = _difference(tmp_path, state, _CONTROL_REFUSED)
    assert result.outcome == "expected_difference"
    assert result.reason_code is EnumOccVerdictDifferenceReason.READBACK_ONLY
    assert result.passed is True
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_REFUSED)


def test_contract_with_no_bound_criterion_reads_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weak contract: a test runs but no criterion is bound to it.

    OMN-20070: the head no longer verifies on the behaviour check alone; it
    reads NO_ACCEPTANCE_CHECKS naming both unbound criteria. The merge-base
    control step finds zero bound checks and leaves its line at ``refused``.
    """
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(["AC1", "AC2"], [_item("dod-1", _TEST_A, [])]),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_basis is EnumDodAcceptanceBasis.NO_ACCEPTANCE_CHECKS
    assert state.acceptance_unbound_criteria == ("AC1", "AC2")
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")
    assert all(not check.binds_ac for check in state.checks)

    result = _difference(tmp_path, state, _CONTROL_REFUSED)
    assert result.outcome == "expected_difference"
    assert (
        result.reason_code
        is EnumOccVerdictDifferenceReason.INCOMPLETE_CRITERION_COVERAGE
    )
    assert result.passed is True
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_REFUSED)


def test_one_unbound_criterion_is_refused_as_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-20070 AC3: the verifier refuses a partly unbound contract.

    AC1 is bound and AC2 is not. The head reads NO_ACCEPTANCE_CHECKS naming
    AC2, so the evidence check refuses even though the control (AC1's bound
    test failing at the base) passes. Before OMN-20070 this read ``verified``
    and only omnimarket's own ``test_every_repo_contract_binds_every_criterion``
    caught it, which a second repository adopting the gate would not carry.
    """
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2"],
            [
                _item("dod-1", _TEST_A, ["AC1"]),
                _item("dod-2", _TEST_B, []),
            ],
        ),
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_unbound_criteria == ("AC2",)
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")
    assert [check.binds_ac for check in state.checks] == [("AC1",), ()]

    result = _difference(tmp_path, state, _CONTROL_PASSED)
    assert result.new_admitted is False
    assert result.outcome == "expected_difference"
    assert (
        result.reason_code
        is EnumOccVerdictDifferenceReason.INCOMPLETE_CRITERION_COVERAGE
    )
    assert result.passed is True
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_PASSED)


def test_failing_bound_test_refuses_with_no_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing bound test refuses; with OCC admitting, the difference is unclassified."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(["AC1"], [_item("dod-1", _TEST_A, ["AC1"])]),
        failing=frozenset({_TEST_A}),
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.error_message is not None
    assert state.error_message.startswith("EVIDENCE_CHECK_FAILED: dod-1: ")

    result = _difference(tmp_path, state, _CONTROL_PASSED)
    assert result.outcome == "unclassified_difference"
    assert result.reason_code is EnumOccVerdictDifferenceReason.UNCLASSIFIED
    assert result.passed is False
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_PASSED)


def test_green_children_with_failing_integration_test_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound integration test fails although the bound unit tests pass."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1", "AC2", "AC3"],
            [
                _item("dod-unit-a", _TEST_A, ["AC1"]),
                _item("dod-unit-b", _TEST_B, ["AC2"]),
                _item("dod-integration", _INTEGRATION, ["AC3"]),
            ],
        ),
        failing=frozenset({_INTEGRATION}),
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    by_id = {check.evidence_id: check.status for check in state.checks}
    assert by_id["dod-unit-a"] is EnumEvidenceCheckStatus.VERIFIED
    assert by_id["dod-unit-b"] is EnumEvidenceCheckStatus.VERIFIED
    assert by_id["dod-integration"] is EnumEvidenceCheckStatus.FAILED

    result = _difference(tmp_path, state, _CONTROL_PASSED)
    assert result.outcome == "unclassified_difference"
    assert result.passed is False
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_PASSED)


def test_unparseable_contract_refuses_with_a_format_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract that does not parse is refused on its format, not by a timeout."""
    state = _verify(tmp_path, monkeypatch, "ticket_id: [unclosed\n  - : :")
    assert state.status is EnumDodVerifyStatus.FAILED
    [check] = state.checks
    assert check.evidence_id == "contract"
    assert check.status is EnumEvidenceCheckStatus.FAILED
    assert check.message is not None
    assert check.message.startswith("YAML parse error")

    result = _difference(tmp_path, state, _CONTROL_REFUSED)
    assert result.new_admitted is False
    assert result.passed is False
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_REFUSED)


def test_self_referential_merge_contract_reads_circular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the merge, a bound check that reads the PR's own merged state fails."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(
            ["AC1"],
            [_item("dod-1", _MERGE_PROBE, ["AC1"], check_type="command")],
        ),
        failing=frozenset({_MERGE_PROBE}),
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    failed = [
        check
        for check in state.checks
        if check.status is EnumEvidenceCheckStatus.FAILED
    ]
    assert [check.proof_class.value for check in failed] == ["merge-state"]

    result = _difference(tmp_path, state, _CONTROL_REFUSED)
    assert result.outcome == "expected_difference"
    assert result.reason_code is EnumOccVerdictDifferenceReason.CIRCULAR_CONTRACT
    assert result.passed is True
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_REFUSED)


def test_always_pass_bound_test_is_refused_by_the_control_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound test passes at the head; the control line says it passed at the base too."""
    state = _verify(
        tmp_path,
        monkeypatch,
        _contract(["AC1"], [_item("dod-1", _TEST_A, ["AC1"])]),
    )
    assert state.status is EnumDodVerifyStatus.VERIFIED

    result = _difference(tmp_path, state, _CONTROL_REFUSED)
    assert result.new_admitted is False
    assert result.outcome == "unclassified_difference"
    assert result.passed is False
    _assert_labelled_control_is_refused(tmp_path, state, _CONTROL_REFUSED)
