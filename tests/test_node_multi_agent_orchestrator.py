# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_multi_agent_orchestrator [OMN-12207].

Wave 2 contract-first stub: verifies importability, model validation,
and that the handler correctly raises NotImplementedError per
contract.yaml `node_not_implemented: true`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_multi_agent_orchestrator import (
    EnumAgentResultStatus,
    EnumConflictClass,
    EnumWorkflowType,
    HandlerMultiAgentOrchestrator,
    ModelAgentResult,
    ModelAgentTask,
    ModelConflictField,
    ModelMultiAgentRequest,
    ModelMultiAgentResult,
    ModelReconciliation,
)

# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    @pytest.mark.unit
    def test_all_symbols_importable(self) -> None:
        assert HandlerMultiAgentOrchestrator is not None
        assert ModelMultiAgentRequest is not None
        assert ModelMultiAgentResult is not None
        assert ModelAgentTask is not None
        assert ModelAgentResult is not None
        assert ModelReconciliation is not None
        assert ModelConflictField is not None
        assert EnumWorkflowType is not None
        assert EnumAgentResultStatus is not None
        assert EnumConflictClass is not None


# ---------------------------------------------------------------------------
# EnumWorkflowType
# ---------------------------------------------------------------------------


class TestEnumWorkflowType:
    @pytest.mark.unit
    def test_all_values_present(self) -> None:
        assert EnumWorkflowType.PARALLEL_DEBUG == "parallel_debug"
        assert EnumWorkflowType.PARALLEL_BUILD == "parallel_build"
        assert EnumWorkflowType.SEQUENTIAL_REVIEW == "sequential_review"


# ---------------------------------------------------------------------------
# ModelAgentTask
# ---------------------------------------------------------------------------


class TestModelAgentTask:
    @pytest.mark.unit
    def test_minimal_task(self) -> None:
        t = ModelAgentTask(task_id="t1", description="Fix failing tests in auth module")
        assert t.task_id == "t1"
        assert t.scope == []
        assert t.depends_on == []
        assert t.prompt_template is None
        assert t.validation_criteria is None

    @pytest.mark.unit
    def test_full_task(self) -> None:
        t = ModelAgentTask(
            task_id="t2",
            description="Implement auth handler",
            scope=["src/auth/handler.py"],
            depends_on=["t1"],
            prompt_template="Fix {scope}",
            validation_criteria="All tests pass",
        )
        assert t.depends_on == ["t1"]
        assert t.scope == ["src/auth/handler.py"]

    @pytest.mark.unit
    def test_frozen(self) -> None:
        t = ModelAgentTask(task_id="t1", description="Task")
        with pytest.raises(ValidationError):
            t.task_id = "t2"  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelAgentTask(task_id="t1", description="T", unknown="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ModelMultiAgentRequest
# ---------------------------------------------------------------------------


class TestModelMultiAgentRequest:
    @pytest.mark.unit
    def test_parallel_debug_request(self) -> None:
        tasks = [
            ModelAgentTask(task_id="t1", description="Fix auth tests"),
            ModelAgentTask(task_id="t2", description="Fix payment tests"),
        ]
        req = ModelMultiAgentRequest(
            workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
            tasks=tasks,
        )
        assert req.workflow_type == EnumWorkflowType.PARALLEL_DEBUG
        assert len(req.tasks) == 2
        assert req.concurrency == 5
        assert req.dry_run is False
        assert req.correlation_id is None

    @pytest.mark.unit
    def test_parallel_build_with_concurrency(self) -> None:
        req = ModelMultiAgentRequest(
            workflow_type=EnumWorkflowType.PARALLEL_BUILD,
            tasks=[ModelAgentTask(task_id="t1", description="Build feature A")],
            concurrency=3,
            dry_run=True,
            correlation_id="corr-abc123",
        )
        assert req.concurrency == 3
        assert req.dry_run is True
        assert req.correlation_id == "corr-abc123"

    @pytest.mark.unit
    def test_sequential_review_request(self) -> None:
        tasks = [
            ModelAgentTask(task_id="t1", description="Task 1"),
            ModelAgentTask(task_id="t2", description="Task 2", depends_on=["t1"]),
        ]
        req = ModelMultiAgentRequest(
            workflow_type=EnumWorkflowType.SEQUENTIAL_REVIEW,
            tasks=tasks,
        )
        assert req.workflow_type == EnumWorkflowType.SEQUENTIAL_REVIEW
        assert req.tasks[1].depends_on == ["t1"]

    @pytest.mark.unit
    def test_concurrency_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelMultiAgentRequest(
                workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
                tasks=[ModelAgentTask(task_id="t1", description="T")],
                concurrency=0,
            )

    @pytest.mark.unit
    def test_concurrency_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelMultiAgentRequest(
                workflow_type=EnumWorkflowType.PARALLEL_BUILD,
                tasks=[ModelAgentTask(task_id="t1", description="T")],
                concurrency=21,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        req = ModelMultiAgentRequest(
            workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
            tasks=[ModelAgentTask(task_id="t1", description="T")],
        )
        with pytest.raises(ValidationError):
            req.concurrency = 10  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelMultiAgentRequest(
                workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
                tasks=[],
                surprise="field",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ModelAgentResult
# ---------------------------------------------------------------------------


class TestModelAgentResult:
    @pytest.mark.unit
    def test_success_result(self) -> None:
        r = ModelAgentResult(
            task_id="t1",
            status=EnumAgentResultStatus.SUCCESS,
            summary="Fixed 3 failing tests in auth module",
            files_changed=["src/auth/handler.py"],
            findings=["Race condition in token refresh"],
        )
        assert r.status == EnumAgentResultStatus.SUCCESS
        assert r.error is None
        assert r.files_changed == ["src/auth/handler.py"]

    @pytest.mark.unit
    def test_failure_result_with_error(self) -> None:
        r = ModelAgentResult(
            task_id="t2",
            status=EnumAgentResultStatus.FAILURE,
            summary="Agent failed to complete task",
            error="Timeout after 120s",
        )
        assert r.status == EnumAgentResultStatus.FAILURE
        assert r.error == "Timeout after 120s"

    @pytest.mark.unit
    def test_frozen(self) -> None:
        r = ModelAgentResult(
            task_id="t1",
            status=EnumAgentResultStatus.SUCCESS,
            summary="Done",
        )
        with pytest.raises(ValidationError):
            r.task_id = "t2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelConflictField
# ---------------------------------------------------------------------------


class TestModelConflictField:
    @pytest.mark.unit
    def test_no_conflict_field(self) -> None:
        f = ModelConflictField(
            field_path="auth.token_ttl",
            conflict_class=EnumConflictClass.NO_CONFLICT,
            competing_values={"agent-1": "3600", "agent-2": "3600"},
            chosen_value="3600",
        )
        assert f.conflict_class == EnumConflictClass.NO_CONFLICT
        assert f.chosen_value == "3600"

    @pytest.mark.unit
    def test_requires_approval_field(self) -> None:
        f = ModelConflictField(
            field_path="auth.strategy",
            conflict_class=EnumConflictClass.REQUIRES_APPROVAL,
            competing_values={"agent-1": "jwt", "agent-2": "session"},
            chosen_value=None,
        )
        assert f.chosen_value is None

    @pytest.mark.unit
    def test_frozen(self) -> None:
        f = ModelConflictField(
            field_path="x",
            conflict_class=EnumConflictClass.NO_CONFLICT,
            competing_values={},
        )
        with pytest.raises(ValidationError):
            f.field_path = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelReconciliation
# ---------------------------------------------------------------------------


class TestModelReconciliation:
    @pytest.mark.unit
    def test_clean_reconciliation(self) -> None:
        r = ModelReconciliation(
            requires_approval=False,
            merged_values={"auth.ttl": "3600"},
        )
        assert r.requires_approval is False
        assert r.approval_required_fields == []
        assert r.optional_review_fields == []

    @pytest.mark.unit
    def test_approval_required(self) -> None:
        conflict = ModelConflictField(
            field_path="auth.strategy",
            conflict_class=EnumConflictClass.REQUIRES_APPROVAL,
            competing_values={"agent-1": "jwt", "agent-2": "session"},
            chosen_value=None,
        )
        r = ModelReconciliation(
            requires_approval=True,
            merged_values={},
            approval_required_fields=[conflict],
        )
        assert r.requires_approval is True
        assert len(r.approval_required_fields) == 1

    @pytest.mark.unit
    def test_frozen(self) -> None:
        r = ModelReconciliation(requires_approval=False)
        with pytest.raises(ValidationError):
            r.requires_approval = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelMultiAgentResult
# ---------------------------------------------------------------------------


class TestModelMultiAgentResult:
    @pytest.mark.unit
    def test_empty_result(self) -> None:
        r = ModelMultiAgentResult(
            workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
            succeeded_count=0,
            failed_count=0,
            skipped_count=0,
            approval_required=False,
        )
        assert r.agent_results == []
        assert r.reconciliation is None
        assert r.total_files_changed == []
        assert r.aggregated_findings == []

    @pytest.mark.unit
    def test_result_with_agents(self) -> None:
        results = [
            ModelAgentResult(
                task_id="t1",
                status=EnumAgentResultStatus.SUCCESS,
                summary="Fixed auth tests",
                files_changed=["auth.py"],
                findings=["Race condition found"],
            ),
            ModelAgentResult(
                task_id="t2",
                status=EnumAgentResultStatus.FAILURE,
                summary="Payment tests failed",
                error="Import error",
            ),
        ]
        r = ModelMultiAgentResult(
            workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
            agent_results=results,
            succeeded_count=1,
            failed_count=1,
            skipped_count=0,
            total_files_changed=["auth.py"],
            aggregated_findings=["[t1] Race condition found"],
            approval_required=False,
        )
        assert r.succeeded_count == 1
        assert r.failed_count == 1
        assert len(r.agent_results) == 2

    @pytest.mark.unit
    def test_counts_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelMultiAgentResult(
                workflow_type=EnumWorkflowType.PARALLEL_BUILD,
                succeeded_count=-1,
                failed_count=0,
                skipped_count=0,
                approval_required=False,
            )


# ---------------------------------------------------------------------------
# Handler stub — must raise NotImplementedError
# ---------------------------------------------------------------------------


class TestHandlerMultiAgentOrchestratorStub:
    @pytest.mark.unit
    def test_handle_raises_not_implemented(self) -> None:
        handler = HandlerMultiAgentOrchestrator()
        req = ModelMultiAgentRequest(
            workflow_type=EnumWorkflowType.PARALLEL_DEBUG,
            tasks=[ModelAgentTask(task_id="t1", description="Fix failing tests")],
        )
        with pytest.raises(NotImplementedError) as exc_info:
            handler.handle(req)
        assert (
            "node_not_implemented" in str(exc_info.value).lower()
            or "wave" in str(exc_info.value).lower()
        )

    @pytest.mark.unit
    def test_handler_instantiates_without_args(self) -> None:
        handler = HandlerMultiAgentOrchestrator()
        assert handler is not None

    @pytest.mark.unit
    def test_stub_raises_for_all_workflow_types(self) -> None:
        handler = HandlerMultiAgentOrchestrator()
        task = ModelAgentTask(task_id="t1", description="Task")
        for wf_type in EnumWorkflowType:
            req = ModelMultiAgentRequest(workflow_type=wf_type, tasks=[task])
            with pytest.raises(NotImplementedError):
                handler.handle(req)
