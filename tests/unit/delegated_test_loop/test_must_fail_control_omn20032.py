# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20032 (GC.9): the must-fail control of a test-run evidence item.

A green test run is a statement about the head. These tests pin the control that
asks whether the PR's changed tests failed on the code before the PR, and pin
that its grade is the delegated test loop's own ``grade_control`` (OMN-19361),
not a second implementation of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.delegated_test_loop.must_fail_control import (
    DIFF_DERIVED_ID_PREFIX,
    ModelMustFailRunRequest,
    ModelMustFailRunResult,
    aggregate_outcome,
    declared_impossible,
    diff_derived_binding,
    evaluate_must_fail_control,
    is_doc_path,
    is_test_side_path,
    pytest_targets,
)
from omnimarket.delegated_test_loop.must_fail_models import (
    EnumMustFailControlOutcome,
    EnumMustFailImpossibleReason,
    ModelPrChangedFile,
    ModelPrDiffFacts,
)

pytestmark = pytest.mark.unit

PRE = "a" * 40
CHANGE = "b" * 40

PASSED_XML = (
    '<testsuite tests="1" failures="0" errors="0" skipped="0">'
    '<testcase classname="tests.test_a" name="test_x"/></testsuite>'
)
FAILED_CALL_XML = (
    '<testsuite tests="1" failures="1" errors="0" skipped="0">'
    '<testcase classname="tests.test_a" name="test_x">'
    '<failure message="assert 1 == 2">tests/test_a.py:4: in test_x\n'
    "E   AssertionError: assert 1 == 2\n"
    "tests/test_a.py:4: AssertionError</failure></testcase></testsuite>"
)
COLLECTION_XML = (
    '<testsuite tests="1" failures="0" errors="1" skipped="0">'
    '<testcase classname="" name="tests.test_a">'
    '<error message="collection failure">ImportError: cannot import name x'
    "</error></testcase></testsuite>"
)


class _Runner:
    """A runner that answers per test path and records what it was asked."""

    def __init__(self, answers: dict[str, tuple[str, int | None]]) -> None:
        self._answers = answers
        self.requests: list[ModelMustFailRunRequest] = []

    def handle(self, request: ModelMustFailRunRequest) -> ModelMustFailRunResult:
        self.requests.append(request)
        xml, code = self._answers[request.test_path]
        return ModelMustFailRunResult(junit_xml=xml, exit_code=code)


def _facts(
    files: list[tuple[str, str]],
    *,
    parent: str = PRE,
    merge: str = CHANGE,
) -> ModelPrDiffFacts:
    return ModelPrDiffFacts(
        repo="OmniNode-ai/omnimarket",
        pr_number=99,
        merge_commit_sha=merge,
        parent_commit_sha=parent,
        changed_files=tuple(ModelPrChangedFile(path=p, status=s) for p, s in files),
    )


def _item(description: str | None = None) -> dict[str, object]:
    return {
        "id": f"{DIFF_DERIVED_ID_PREFIX}-pr-99",
        "description": description
        or "PR #99 on OmniNode-ai/omnimarket — diff-derived behavior proof (OMN-16434).",
        "checks": [
            {
                "check_type": "test_passes",
                "check_value": "uv run pytest tests/test_a.py -q",
                "cwd": "${OMNI_HOME}/omnimarket",
            }
        ],
    }


_SRC_AND_TEST = [
    ("src/pkg/mod.py", "modified"),
    ("tests/test_a.py", "modified"),
]


def _evaluate(
    facts: ModelPrDiffFacts | None,
    runner: _Runner | None,
    *,
    command: str = "uv run pytest tests/test_a.py -q",
    item: dict[str, object] | None = None,
    repo_dir: Path | None = Path("/repo"),
):
    return evaluate_must_fail_control(
        item=item or _item(),
        command=command,
        facts=facts,
        runner=runner,
        repo_dir=repo_dir,
        timeout_seconds=30,
    )


class TestGrade:
    def test_assertion_failure_before_the_change_is_the_headline(self) -> None:
        runner = _Runner({"tests/test_a.py": (FAILED_CALL_XML, 1)})
        control = _evaluate(_facts(_SRC_AND_TEST), runner)
        assert control.outcome is EnumMustFailControlOutcome.CONTROLLED
        assert control.headline is True
        assert control.grade_status == "accepted_call"
        assert control.route == "control"
        assert [(r.path, r.outcome) for r in control.runs] == [
            ("tests/test_a.py", "failed_call")
        ]

    def test_a_test_that_passes_before_the_change_is_vacuous(self) -> None:
        runner = _Runner({"tests/test_a.py": (PASSED_XML, 0)})
        control = _evaluate(_facts(_SRC_AND_TEST), runner)
        assert control.outcome is EnumMustFailControlOutcome.VACUOUS
        assert control.headline is False
        assert control.grade_status == "control_did_not_fail"

    def test_a_collection_only_failure_is_weak_and_not_headline(self) -> None:
        runner = _Runner({"tests/test_a.py": (COLLECTION_XML, 2)})
        control = _evaluate(_facts(_SRC_AND_TEST), runner)
        assert control.outcome is EnumMustFailControlOutcome.CONTROLLED_WEAK
        assert control.headline is False
        assert control.grade_status == "accepted_collection"

    def test_a_run_that_never_produced_a_verdict_is_unavailable_not_a_pass(
        self,
    ) -> None:
        runner = _Runner({"tests/test_a.py": ("", None)})
        control = _evaluate(_facts(_SRC_AND_TEST), runner)
        assert control.outcome is EnumMustFailControlOutcome.UNAVAILABLE

    def test_one_discriminating_file_outranks_a_vacuous_sibling(self) -> None:
        item = _item()
        runner = _Runner(
            {
                "tests/test_a.py": (PASSED_XML, 0),
                "tests/test_b.py": (FAILED_CALL_XML, 1),
            }
        )
        control = _evaluate(
            _facts(
                [
                    ("src/pkg/mod.py", "modified"),
                    ("tests/test_a.py", "modified"),
                    ("tests/test_b.py", "added"),
                ]
            ),
            runner,
            command="uv run pytest tests/test_a.py tests/test_b.py -q",
            item=item,
        )
        assert control.outcome is EnumMustFailControlOutcome.CONTROLLED
        assert {r.path: r.outcome for r in control.runs} == {
            "tests/test_a.py": "passed",
            "tests/test_b.py": "failed_call",
        }

    def test_the_same_commit_twice_never_grades_controlled(self) -> None:
        runner = _Runner({"tests/test_a.py": (FAILED_CALL_XML, 1)})
        control = _evaluate(_facts(_SRC_AND_TEST, parent=CHANGE), runner)
        assert control.outcome is not EnumMustFailControlOutcome.CONTROLLED
        assert control.headline is False

    def test_the_grade_is_the_delegated_test_loops_compute(self) -> None:
        """Pin the reuse: the control's statuses are the compute's statuses."""
        from omnimarket.nodes.node_delegated_test_control_compute import (
            EnumControlStatus,
        )

        for xml, code, status in (
            (FAILED_CALL_XML, 1, EnumControlStatus.ACCEPTED_CALL),
            (COLLECTION_XML, 2, EnumControlStatus.ACCEPTED_COLLECTION),
            (PASSED_XML, 0, EnumControlStatus.CONTROL_DID_NOT_FAIL),
        ):
            control = _evaluate(
                _facts(_SRC_AND_TEST), _Runner({"tests/test_a.py": (xml, code)})
            )
            assert control.grade_status == status.value


class TestRunRequest:
    def test_each_target_runs_at_the_pre_change_commit_with_test_side_overlay(
        self,
    ) -> None:
        runner = _Runner({"tests/test_a.py": (FAILED_CALL_XML, 1)})
        _evaluate(
            _facts(
                [
                    ("src/pkg/mod.py", "modified"),
                    ("tests/test_a.py", "modified"),
                    ("tests/conftest.py", "modified"),
                    ("tests/fixtures/data.json", "added"),
                    ("tests/test_gone.py", "removed"),
                ]
            ),
            runner,
        )
        (request,) = runner.requests
        assert request.pre_change_sha == PRE
        assert request.change_sha == CHANGE
        assert request.test_path == "tests/test_a.py"
        assert request.overlay_paths == (
            "tests/conftest.py",
            "tests/fixtures/data.json",
            "tests/test_a.py",
        )

    def test_source_files_are_never_overlaid(self) -> None:
        runner = _Runner({"tests/test_a.py": (FAILED_CALL_XML, 1)})
        _evaluate(_facts(_SRC_AND_TEST), runner)
        assert "src/pkg/mod.py" not in runner.requests[0].overlay_paths


class TestImpossible:
    """Each impossible case names its reason and never runs a test."""

    @pytest.mark.parametrize(
        ("files", "reason"),
        [
            (
                [("docs/guide.md", "modified"), ("README.md", "modified")],
                EnumMustFailImpossibleReason.DOCS_ONLY_DIFF,
            ),
            (
                [("tests/test_a.py", "modified"), ("tests/conftest.py", "modified")],
                EnumMustFailImpossibleReason.TEST_ONLY_DIFF,
            ),
            (
                [("src/pkg/mod.py", "modified"), ("tests/test_other.py", "modified")],
                EnumMustFailImpossibleReason.NO_TEST_FILE_IN_DIFF,
            ),
        ],
    )
    def test_diff_shapes_with_no_possible_control(
        self, files: list[tuple[str, str]], reason: EnumMustFailImpossibleReason
    ) -> None:
        runner = _Runner({})
        control = _evaluate(_facts(files), runner)
        assert control.outcome is EnumMustFailControlOutcome.IMPOSSIBLE
        assert control.impossible_reason is reason
        assert control.reason
        assert runner.requests == []

    def test_a_root_commit_has_no_earlier_code(self) -> None:
        runner = _Runner({})
        control = _evaluate(_facts(_SRC_AND_TEST, parent=""), runner)
        assert control.impossible_reason is (
            EnumMustFailImpossibleReason.NO_PRE_CHANGE_COMMIT
        )
        assert runner.requests == []

    def test_an_item_can_declare_a_pure_refactor_with_a_reason(self) -> None:
        item = _item(
            "PR #99 on OmniNode-ai/omnimarket — diff-derived behavior proof.\n"
            "must-fail-control: impossible (pure-refactor): moves the helper "
            "between modules and changes no behaviour"
        )
        runner = _Runner({})
        control = _evaluate(_facts(_SRC_AND_TEST), runner, item=item)
        assert (
            control.impossible_reason is EnumMustFailImpossibleReason.DECLARED_BY_ITEM
        )
        assert "moves the helper" in control.reason
        assert runner.requests == []

    def test_a_declaration_without_a_reason_declares_nothing(self) -> None:
        assert (
            declared_impossible("must-fail-control: impossible (pure-refactor): ")
            is None
        )
        assert (
            declared_impossible("must-fail-control: impossible (pure-refactor): short")
            is None
        )
        assert (
            declared_impossible("impossible (pure-refactor): a long enough reason")
            is None
        )

    def test_a_command_that_is_not_a_test_file_list(self) -> None:
        control = _evaluate(
            _facts(_SRC_AND_TEST), _Runner({}), command="gh pr view 99 --json state"
        )
        assert control.impossible_reason is (
            EnumMustFailImpossibleReason.COMMAND_NOT_A_TEST_FILE_LIST
        )


class TestUnavailable:
    def test_unreadable_diff(self) -> None:
        control = _evaluate(None, _Runner({}))
        assert control.outcome is EnumMustFailControlOutcome.UNAVAILABLE

    def test_unmerged_pr(self) -> None:
        facts = ModelPrDiffFacts(repo="OmniNode-ai/omnimarket", pr_number=99)
        control = _evaluate(facts, _Runner({}))
        assert control.outcome is EnumMustFailControlOutcome.UNAVAILABLE
        assert "not merged" in control.reason

    def test_no_runner(self) -> None:
        control = _evaluate(_facts(_SRC_AND_TEST), None)
        assert control.outcome is EnumMustFailControlOutcome.UNAVAILABLE


class TestParsing:
    def test_binding_comes_from_the_id_and_the_generated_description(self) -> None:
        assert diff_derived_binding(_item()) == ("OmniNode-ai/omnimarket", 99)

    def test_an_item_with_the_id_and_no_bound_description_is_not_bound(self) -> None:
        item = _item("diff-derived behavior proof")
        assert diff_derived_binding(item) is None

    def test_another_item_is_not_diff_derived(self) -> None:
        item = _item()
        item["id"] = "dod-001"
        assert diff_derived_binding(item) is None

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("uv run pytest tests/test_a.py -q", ("tests/test_a.py",)),
            (
                "pytest tests/test_a.py tests/test_b.py -q",
                ("tests/test_a.py", "tests/test_b.py"),
            ),
            ("uv run pytest tests/ -q", None),
            ("uv run pytest tests/test_a.py::test_x -q", None),
            ("uv run pytest tests/test_a.py -q -k foo", None),
            ("uv run pytest /etc/passwd.py -q", None),
            ("uv run pytest ../x/test_a.py -q", None),
            ("uv run pytest -q", None),
            ("echo pytest tests/test_a.py", None),
        ],
    )
    def test_pytest_targets(
        self, command: str, expected: tuple[str, ...] | None
    ) -> None:
        assert pytest_targets(command) == expected

    def test_the_id_prefix_is_the_producers_constant(self) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
            BEHAVIOR_PROOF_EVIDENCE_ID,
        )

        assert DIFF_DERIVED_ID_PREFIX == BEHAVIOR_PROOF_EVIDENCE_ID

    def test_path_classes(self) -> None:
        assert is_test_side_path("tests/conftest.py")
        assert is_test_side_path("tests/fixtures/x.json")
        assert is_test_side_path("src/pkg/tests/test_x.py")
        assert is_test_side_path("test_top.py")
        assert not is_test_side_path("src/pkg/mod.py")
        assert is_doc_path("docs/a.yaml")
        assert is_doc_path("README.md")
        assert not is_doc_path("src/pkg/mod.py")

    def test_aggregate_prefers_the_strongest_evidence(self) -> None:
        assert (
            aggregate_outcome(["passed", "failed_call", "infra_error"]) == "failed_call"
        )
        assert aggregate_outcome(["passed", "failed_collection"]) == "failed_collection"
        assert aggregate_outcome(["passed", "infra_error"]) == "infra_error"
        assert aggregate_outcome(["passed", "passed"]) == "passed"
        assert aggregate_outcome([]) == "infra_error"
