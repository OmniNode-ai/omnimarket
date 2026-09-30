# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20032 (GC.9): the verifier records a must-fail control on a passing test run.

``test_passes`` exits 0 when the tests pass at the head. A test that also passes
on the code before the change asserts nothing about the change, so the verifier
re-runs a diff-derived item's changed tests at the code before the PR and
records what happened on the item's result.

"Loop route" below is the route through the delegated test loop's control grade
(``grade_control``, OMN-19361); "shell route" is the command alone. The receipt
says which ran, and a shell-route result is never labelled controlled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.delegated_test_loop.must_fail_control import (
    DIFF_DERIVED_ID_PREFIX,
    ModelMustFailRunRequest,
    ModelMustFailRunResult,
)
from omnimarket.delegated_test_loop.must_fail_models import (
    EnumMustFailControlOutcome,
    EnumMustFailImpossibleReason,
    ModelPrChangedFile,
    ModelPrDiffFacts,
)
from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
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
    EnumEvidenceUnverifiableCause,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

PRE = "a" * 40
CHANGE = "b" * 40
FAILED = (
    '<testsuite tests="1" failures="1" errors="0" skipped="0">'
    '<testcase classname="tests.test_a" name="test_x"><failure message="assert 1 == 2">'
    "tests/test_a.py:4: AssertionError</failure></testcase></testsuite>"
)
PASSED = (
    '<testsuite tests="1" failures="0" errors="0" skipped="0">'
    '<testcase classname="tests.test_a" name="test_x"/></testsuite>'
)


class _Runner:
    def __init__(self, xml: str, code: int | None) -> None:
        self._xml, self._code = xml, code
        self.requests: list[ModelMustFailRunRequest] = []

    def handle(self, request: ModelMustFailRunRequest) -> ModelMustFailRunResult:
        self.requests.append(request)
        return ModelMustFailRunResult(junit_xml=self._xml, exit_code=self._code)


def _diff_item() -> dict[str, Any]:
    return {
        "id": f"{DIFF_DERIVED_ID_PREFIX}-pr-99",
        "description": "PR #99 on OmniNode-ai/omnimarket — diff-derived behavior proof (OMN-16434).",
        "checks": [
            {
                "check_type": "test_passes",
                "check_value": "uv run pytest tests/test_a.py -q",
                "cwd": "${OMNI_HOME}/omnimarket",
            }
        ],
    }


def _facts(files: list[tuple[str, str]]) -> ModelPrDiffFacts:
    return ModelPrDiffFacts(
        repo="OmniNode-ai/omnimarket",
        pr_number=99,
        merge_commit_sha=CHANGE,
        parent_commit_sha=PRE,
        changed_files=tuple(ModelPrChangedFile(path=p, status=s) for p, s in files),
    )


_SRC_AND_TEST = [("src/pkg/mod.py", "modified"), ("tests/test_a.py", "modified")]


def _green(
    item: dict[str, Any], status: EnumEvidenceCheckStatus
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=str(item["id"]),
        description=str(item["description"]),
        status=status,
        message="OK: 1 passed",
        proof_class=EnumCheckProofClass.BEHAVIOR,
    )


def _run_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    item: dict[str, Any],
    *,
    runner: _Runner | None,
    facts: ModelPrDiffFacts | None,
    head_status: EnumEvidenceCheckStatus = EnumEvidenceCheckStatus.VERIFIED,
) -> list[ModelEvidenceCheckResult]:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    (tmp_path / "omnimarket").mkdir(exist_ok=True)
    collector = EvidenceCollector(must_fail_runner=runner)
    monkeypatch.setattr(
        collector, "_check_evidence_item", lambda *_a, **_k: _green(item, head_status)
    )
    monkeypatch.setattr(collector, "_live_pr_checks_for_item", lambda *_a, **_k: [])
    monkeypatch.setattr(collector, "_fetch_pr_diff_facts", lambda *_a, **_k: facts)
    return collector._execute_item(
        item,
        "OMN-20032",
        None,
        0,
        EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
        None,
        None,
    )


def _verdict(results: list[ModelEvidenceCheckResult]):  # type: ignore[no-untyped-def]
    return HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id="OMN-20032"), evidence_results=results
    )


def test_loop_route_records_control_on_a_diff_derived_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _Runner(FAILED, 1)
    (result, *_) = _run_item(
        monkeypatch, tmp_path, _diff_item(), runner=runner, facts=_facts(_SRC_AND_TEST)
    )
    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    control = result.must_fail_control
    assert control is not None
    assert control.route == "control"
    assert control.outcome is EnumMustFailControlOutcome.CONTROLLED
    assert control.headline is True
    assert (control.pre_change_sha, control.change_sha) == (PRE, CHANGE)
    assert runner.requests[0].repo_dir == tmp_path / "omnimarket"
    state = _verdict([result])
    assert state.behavior_proving_count == 1


def test_a_vacuous_test_is_not_probative_and_is_not_behavior_proving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        _diff_item(),
        runner=_Runner(PASSED, 0),
        facts=_facts(_SRC_AND_TEST),
    )
    assert result.status is EnumEvidenceCheckStatus.NON_PROBATIVE
    assert result.must_fail_control is not None
    assert result.must_fail_control.outcome is EnumMustFailControlOutcome.VACUOUS
    assert "MUST_FAIL_CONTROL vacuous" in (result.message or "")
    state = _verdict([result])
    assert state.behavior_proving_count == 0
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_an_unrunnable_control_blocks_and_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        _diff_item(),
        runner=_Runner("", None),
        facts=_facts(_SRC_AND_TEST),
    )
    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert result.unverifiable_cause is (
        EnumEvidenceUnverifiableCause.MUST_FAIL_CONTROL_UNAVAILABLE
    )
    state = _verdict([result])
    assert state.behavior_proving_count == 0
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_an_unreadable_diff_blocks_rather_than_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (result, *_) = _run_item(
        monkeypatch, tmp_path, _diff_item(), runner=_Runner(FAILED, 1), facts=None
    )
    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert result.must_fail_control is not None
    assert result.must_fail_control.outcome is EnumMustFailControlOutcome.UNAVAILABLE


def test_an_impossible_control_keeps_the_verdict_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _Runner(FAILED, 1)
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        _diff_item(),
        runner=runner,
        facts=_facts([("docs/guide.md", "modified")]),
    )
    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    control = result.must_fail_control
    assert control is not None
    assert control.outcome is EnumMustFailControlOutcome.IMPOSSIBLE
    assert control.impossible_reason is EnumMustFailImpossibleReason.DOCS_ONLY_DIFF
    assert control.headline is False
    assert runner.requests == []


def test_shell_route_not_controlled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A test run that is not a diff-derived proof runs by the shell alone."""
    item = {
        "id": "dod-001",
        "description": "the unit tests pass",
        "checks": [
            {
                "check_type": "test_passes",
                "check_value": "uv run pytest tests/test_a.py -q",
                "cwd": "${OMNI_HOME}/omnimarket",
            }
        ],
    }
    runner = _Runner(FAILED, 1)
    (result, *_) = _run_item(
        monkeypatch, tmp_path, item, runner=runner, facts=_facts(_SRC_AND_TEST)
    )
    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    control = result.must_fail_control
    assert control is not None
    assert control.route == "shell"
    assert control.outcome is EnumMustFailControlOutcome.IMPOSSIBLE
    assert control.outcome is not EnumMustFailControlOutcome.CONTROLLED
    assert control.headline is False
    assert control.grade_status == ""
    assert runner.requests == []


def test_an_item_with_the_id_and_no_bound_description_is_shell_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = _diff_item()
    item["description"] = "diff-derived behavior proof"
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        item,
        runner=_Runner(FAILED, 1),
        facts=_facts(_SRC_AND_TEST),
    )
    assert result.must_fail_control is not None
    assert result.must_fail_control.route == "shell"


def test_a_red_head_check_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _Runner(FAILED, 1)
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        _diff_item(),
        runner=runner,
        facts=_facts(_SRC_AND_TEST),
        head_status=EnumEvidenceCheckStatus.FAILED,
    )
    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.must_fail_control is None
    assert runner.requests == []


def test_a_command_item_carries_no_control_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = {
        "id": "dod-002",
        "description": "a command",
        "checks": [{"check_type": "command", "check_value": "true"}],
    }
    (result, *_) = _run_item(
        monkeypatch, tmp_path, item, runner=None, facts=_facts(_SRC_AND_TEST)
    )
    assert result.must_fail_control is None
    assert "must_fail_control" not in result.model_dump(mode="json")


def test_the_record_survives_the_receipt_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (result, *_) = _run_item(
        monkeypatch,
        tmp_path,
        _diff_item(),
        runner=_Runner(FAILED, 1),
        facts=_facts(_SRC_AND_TEST),
    )
    dumped = result.model_dump(mode="json")
    assert dumped["must_fail_control"]["outcome"] == "controlled"
    assert ModelEvidenceCheckResult.model_validate(dumped) == result
