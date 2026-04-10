"""Phase-level tests for HandlerTicketWork with stub Protocol implementations.

Tests each phase method (run_intake, run_research, run_questions, run_spec,
run_implement, run_review, run_done) using lightweight stub clients.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_ticket_work.handlers.handler_ticket_work import (
    HandlerTicketWork,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_contract import (
    ModelContractContext,
    ModelContractQuestion,
    ModelContractRequirement,
    ModelTicketContract,
    extract_contract,
    update_description_with_contract,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_command import (
    ModelTicketWorkCommand,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    EnumTicketWorkPhase,
)
from omnimarket.nodes.node_ticket_work.protocols.protocol_git_client import (
    ModelRunResult,
    ModelWorktreeInfo,
)
from omnimarket.nodes.node_ticket_work.protocols.protocol_linear_client import (
    ModelLinearIssue,
    ModelLinearStateInfo,
)

# ---------------------------------------------------------------------------
# Stub implementations
# ---------------------------------------------------------------------------


class StubLinearClient:
    """Stub Linear client for unit tests."""

    def __init__(
        self,
        description: str = "",
        branch_name: str = "jonah/omn-1234-test",
        state: str = "Todo",
        team_id: str = "team-1",
    ) -> None:
        self._issue = ModelLinearIssue(
            id="OMN-1234",
            title="Test ticket",
            description=description,
            branch_name=branch_name,
            state=state,
            team_id=team_id,
        )
        self.updated_descriptions: list[str] = []
        self.updated_states: list[str] = []

    def get_issue(self, ticket_id: str) -> ModelLinearIssue:
        return self._issue

    def update_issue_description(self, ticket_id: str, description: str) -> None:
        self._issue = self._issue.model_copy(update={"description": description})
        self.updated_descriptions.append(description)

    def update_issue_state(self, ticket_id: str, state_name: str) -> bool:
        self.updated_states.append(state_name)
        return True

    def list_states(self, team_id: str) -> list[ModelLinearStateInfo]:
        return [
            ModelLinearStateInfo(id="s1", name="Todo", type="unstarted"),
            ModelLinearStateInfo(id="s2", name="In Progress", type="started"),
            ModelLinearStateInfo(id="s3", name="In Review", type="started"),
            ModelLinearStateInfo(id="s4", name="Done", type="completed"),
        ]


class StubGitClient:
    """Stub git client for unit tests."""

    def __init__(
        self,
        test_exit_code: int = 0,
        pre_commit_exit_code: int = 0,
        push_exit_code: int = 0,
        pr_url: str = "https://github.com/org/repo/pull/42",
    ) -> None:
        self._test_exit_code = test_exit_code
        self._pre_commit_exit_code = pre_commit_exit_code
        self._push_exit_code = push_exit_code
        self._pr_url = pr_url
        self.pre_commit_installed: list[str] = []

    def create_or_checkout_worktree(
        self, repo_path: str, ticket_id: str, branch_name: str
    ) -> ModelWorktreeInfo:
        return ModelWorktreeInfo(
            path=f"/tmp/worktrees/{ticket_id}",
            branch=branch_name or f"jonah/{ticket_id.lower()}-test",
            created=True,
        )

    def install_pre_commit(self, worktree_path: str) -> bool:
        self.pre_commit_installed.append(worktree_path)
        return True

    def run_pre_commit(self, worktree_path: str) -> ModelRunResult:
        return ModelRunResult(
            command="pre-commit run --all-files",
            exit_code=self._pre_commit_exit_code,
            stdout="All checks passed." if self._pre_commit_exit_code == 0 else "",
            stderr="" if self._pre_commit_exit_code == 0 else "pre-commit failure",
        )

    def run_tests(self, worktree_path: str) -> ModelRunResult:
        return ModelRunResult(
            command="uv run pytest tests/",
            exit_code=self._test_exit_code,
            stdout="5 passed" if self._test_exit_code == 0 else "",
            stderr="" if self._test_exit_code == 0 else "test failure",
        )

    def commit_changes(self, worktree_path: str, message: str) -> ModelRunResult:
        return ModelRunResult(
            command=f"git commit -m '{message}'",
            exit_code=0,
            stdout="abc1234",
        )

    def push_branch(self, worktree_path: str, branch: str) -> ModelRunResult:
        return ModelRunResult(
            command=f"git push -u origin {branch}",
            exit_code=self._push_exit_code,
            stdout="" if self._push_exit_code != 0 else "pushed",
            stderr="" if self._push_exit_code == 0 else "push failed",
        )

    def create_pr(self, worktree_path: str, title: str, body: str) -> ModelRunResult:
        return ModelRunResult(
            command="gh pr create",
            exit_code=0,
            stdout=self._pr_url,
        )


def _make_command(
    ticket_id: str = "OMN-1234",
    autonomous: bool = False,
    dry_run: bool = False,
) -> ModelTicketWorkCommand:
    return ModelTicketWorkCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        autonomous=autonomous,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _base_contract(phase: str = "intake") -> ModelTicketContract:
    return ModelTicketContract(
        ticket_id="OMN-1234",
        title="Test ticket",
        repo="omnimarket",
        phase=phase,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Contract model tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelTicketContract:
    """Tests for ModelTicketContract helpers."""

    def test_extract_contract_from_description(self) -> None:
        """extract_contract parses YAML block from ticket description."""
        contract = _base_contract()
        description = "Some description.\n"
        updated_desc = update_description_with_contract(description, contract)

        parsed = extract_contract(updated_desc)
        assert parsed is not None
        assert parsed.ticket_id == "OMN-1234"
        assert parsed.title == "Test ticket"

    def test_extract_contract_returns_none_when_missing(self) -> None:
        assert extract_contract("No contract here.") is None

    def test_update_description_appends_when_no_contract(self) -> None:
        desc = "Original content."
        updated = update_description_with_contract(desc, _base_contract())
        assert "## Contract" in updated
        assert "Original content." in updated

    def test_update_description_replaces_existing_contract(self) -> None:
        contract = _base_contract()
        desc = update_description_with_contract("Initial.", contract)
        updated_contract = contract.model_copy(update={"title": "Updated title"})
        new_desc = update_description_with_contract(desc, updated_contract)
        # Should not duplicate the ## Contract section
        assert new_desc.count("## Contract") == 1
        assert "Updated title" in new_desc

    def test_is_questions_complete_all_answered(self) -> None:
        contract = _base_contract().model_copy(
            update={
                "questions": [
                    ModelContractQuestion(
                        id="q1", text="Q?", required=True, answer="A"
                    ),
                ]
            }
        )
        assert contract.is_questions_complete() is True

    def test_is_questions_complete_unanswered_required(self) -> None:
        contract = _base_contract().model_copy(
            update={
                "questions": [
                    ModelContractQuestion(
                        id="q1", text="Q?", required=True, answer=None
                    ),
                ]
            }
        )
        assert contract.is_questions_complete() is False

    def test_is_questions_complete_empty(self) -> None:
        assert _base_contract().is_questions_complete() is True

    def test_is_spec_complete(self) -> None:
        contract = _base_contract().model_copy(
            update={
                "requirements": [
                    ModelContractRequirement(
                        id="r1", statement="Do thing", acceptance=["it works"]
                    )
                ]
            }
        )
        assert contract.is_spec_complete() is True

    def test_is_spec_complete_empty_requirements(self) -> None:
        assert _base_contract().is_spec_complete() is False


# ---------------------------------------------------------------------------
# Phase handler tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunIntake:
    """Tests for HandlerTicketWork.run_intake."""

    def test_dry_run_creates_stub_contract(self) -> None:
        handler = HandlerTicketWork()
        contract, success, error = handler.run_intake("OMN-9999", dry_run=True)
        assert success is True
        assert error is None
        assert contract.ticket_id == "OMN-9999"
        assert contract.phase == "research"

    def test_real_creates_new_contract(self) -> None:
        stub = StubLinearClient(description="No contract here.")
        handler = HandlerTicketWork(linear_client=stub)
        contract, success, _error = handler.run_intake("OMN-1234")
        assert success is True
        assert contract.ticket_id == "OMN-1234"
        assert contract.phase == "research"
        # Should have persisted to Linear
        assert len(stub.updated_descriptions) == 1
        assert "## Contract" in stub.updated_descriptions[0]

    def test_real_resumes_existing_contract(self) -> None:
        existing = _base_contract(phase="spec")
        desc_with_contract = update_description_with_contract("Desc.", existing)
        stub = StubLinearClient(description=desc_with_contract)
        handler = HandlerTicketWork(linear_client=stub)

        contract, success, _error = handler.run_intake("OMN-1234")
        assert success is True
        assert contract.phase == "spec"  # resumed, not overwritten

    def test_linear_failure_returns_error(self) -> None:
        class FailingLinear:
            def get_issue(self, ticket_id: str) -> None:
                raise RuntimeError("API error")

            def update_issue_description(self, *a: object, **kw: object) -> None:
                pass

            def update_issue_state(self, *a: object, **kw: object) -> bool:
                return False

            def list_states(self, *a: object, **kw: object) -> list:
                return []

        handler = HandlerTicketWork(linear_client=FailingLinear())  # type: ignore[arg-type]
        _, success, error = handler.run_intake("OMN-1234")
        assert success is False
        assert error is not None
        assert "Linear fetch failed" in error


@pytest.mark.unit
class TestRunResearch:
    """Tests for HandlerTicketWork.run_research."""

    def test_dry_run_advances_with_stub_context(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_research(_base_contract(), dry_run=True)
        assert success is True
        assert contract.phase == "questions"
        assert contract.context.relevant_files == ["src/placeholder.py"]

    def test_real_advances_phase(self) -> None:
        stub = StubLinearClient()
        handler = HandlerTicketWork(linear_client=stub)
        base = _base_contract().model_copy(
            update={
                "context": ModelContractContext(
                    relevant_files=["src/foo.py"],
                    notes="Found pattern X",
                )
            }
        )
        contract, success, _error = handler.run_research(base)
        assert success is True
        assert contract.phase == "questions"


@pytest.mark.unit
class TestRunQuestions:
    """Tests for HandlerTicketWork.run_questions."""

    def test_no_questions_advances(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_questions(_base_contract())
        assert success is True
        assert contract.phase == "spec"

    def test_unanswered_required_blocks(self) -> None:
        handler = HandlerTicketWork()
        base = _base_contract().model_copy(
            update={
                "questions": [
                    ModelContractQuestion(
                        id="q1", text="What?", required=True, answer=None
                    ),
                ]
            }
        )
        _contract, success, error = handler.run_questions(base, autonomous=False)
        assert success is False
        assert error is not None
        assert "unanswered" in error

    def test_unanswered_required_autonomous_advances(self) -> None:
        handler = HandlerTicketWork()
        base = _base_contract().model_copy(
            update={
                "questions": [
                    ModelContractQuestion(
                        id="q1", text="What?", required=True, answer=None
                    ),
                ]
            }
        )
        contract, success, _error = handler.run_questions(base, autonomous=True)
        assert success is True
        assert contract.phase == "spec"

    def test_answered_questions_advances(self) -> None:
        handler = HandlerTicketWork()
        base = _base_contract().model_copy(
            update={
                "questions": [
                    ModelContractQuestion(
                        id="q1", text="What?", required=True, answer="Use OAuth2"
                    ),
                ]
            }
        )
        contract, success, _error = handler.run_questions(base)
        assert success is True
        assert contract.phase == "spec"


@pytest.mark.unit
class TestRunSpec:
    """Tests for HandlerTicketWork.run_spec."""

    def test_injects_default_requirements_when_empty(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_spec(_base_contract(), dry_run=True)
        assert success is True
        assert len(contract.requirements) == 1
        assert contract.requirements[0].id == "r1"

    def test_preserves_existing_requirements(self) -> None:
        handler = HandlerTicketWork()
        base = _base_contract().model_copy(
            update={
                "requirements": [
                    ModelContractRequirement(
                        id="r-custom", statement="Custom req", acceptance=["done"]
                    )
                ]
            }
        )
        contract, success, _error = handler.run_spec(base, dry_run=True)
        assert success is True
        assert len(contract.requirements) == 1
        assert contract.requirements[0].id == "r-custom"

    def test_autonomous_auto_approves_gates(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_spec(
            _base_contract(), autonomous=True, dry_run=True
        )
        assert success is True
        assert all(g.status == "approved" for g in contract.gates)

    def test_non_autonomous_leaves_gates_pending(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_spec(
            _base_contract(), autonomous=False, dry_run=True
        )
        assert success is True
        assert all(g.status == "pending" for g in contract.gates)

    def test_injects_verification_steps_when_empty(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_spec(_base_contract(), dry_run=True)
        assert success is True
        assert len(contract.verification) == 3


@pytest.mark.unit
class TestRunImplement:
    """Tests for HandlerTicketWork.run_implement."""

    def test_dry_run_records_stub_branch(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_implement(
            _base_contract(),
            ticket_id="OMN-1234",
            branch_name="jonah/omn-1234",
            dry_run=True,
        )
        assert success is True
        assert contract.branch == "jonah/omn-1234"
        assert contract.commits == ["dry-run-sha"]
        assert contract.phase == "review"

    def test_real_creates_worktree_and_installs_precommit(self) -> None:
        stub_git = StubGitClient()
        handler = HandlerTicketWork(git_client=stub_git)
        contract, success, _error = handler.run_implement(
            _base_contract(),
            ticket_id="OMN-1234",
            branch_name="jonah/omn-1234",
            dry_run=False,
        )
        assert success is True
        assert contract.branch == "jonah/omn-1234"
        assert contract.phase == "review"
        assert len(stub_git.pre_commit_installed) == 1


@pytest.mark.unit
class TestRunReview:
    """Tests for HandlerTicketWork.run_review."""

    def test_dry_run_sets_stub_pr_url(self) -> None:
        handler = HandlerTicketWork()
        contract, success, _error = handler.run_review(_base_contract(), dry_run=True)
        assert success is True
        assert contract.pr_url is not None
        assert contract.phase == "done"

    def test_real_creates_pr_and_updates_linear(self) -> None:
        stub_git = StubGitClient()
        stub_linear = StubLinearClient()
        handler = HandlerTicketWork(linear_client=stub_linear, git_client=stub_git)
        base = _base_contract(phase="review").model_copy(
            update={"branch": "jonah/omn-1234-test"}
        )
        contract, success, _error = handler.run_review(base)
        assert success is True
        assert contract.pr_url == "https://github.com/org/repo/pull/42"
        assert "In Review" in stub_linear.updated_states

    def test_precommit_failure_blocks(self) -> None:
        stub_git = StubGitClient(pre_commit_exit_code=1)
        stub_linear = StubLinearClient()
        handler = HandlerTicketWork(linear_client=stub_linear, git_client=stub_git)
        base = _base_contract(phase="review").model_copy(
            update={"branch": "jonah/omn-1234-test"}
        )
        _contract, success, error = handler.run_review(base)
        assert success is False
        assert error is not None
        assert "pre-commit failed" in error

    def test_push_failure_blocks(self) -> None:
        stub_git = StubGitClient(push_exit_code=1)
        stub_linear = StubLinearClient()
        handler = HandlerTicketWork(linear_client=stub_linear, git_client=stub_git)
        base = _base_contract(phase="review").model_copy(
            update={"branch": "jonah/omn-1234-test"}
        )
        _contract, success, error = handler.run_review(base)
        assert success is False
        assert error is not None
        assert "git push failed" in error


@pytest.mark.unit
class TestRunDone:
    """Tests for HandlerTicketWork.run_done."""

    def test_marks_phase_done(self) -> None:
        handler = HandlerTicketWork()
        contract, success, error = handler.run_done(_base_contract(), dry_run=True)
        assert success is True
        assert contract.phase == "done"
        assert error is None


@pytest.mark.unit
class TestFullPipelineDryRun:
    """Full pipeline tests in dry-run mode (no external I/O)."""

    def test_dry_run_completes_all_phases(self) -> None:
        """Dry-run with no clients completes all 7 phases."""
        handler = HandlerTicketWork()
        command = _make_command(dry_run=True)
        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumTicketWorkPhase.DONE
        assert completed.final_phase == EnumTicketWorkPhase.DONE
        assert len(events) == 7

    def test_dry_run_autonomous_completes(self) -> None:
        """Dry-run autonomous mode completes without human gates."""
        handler = HandlerTicketWork()
        command = _make_command(dry_run=True, autonomous=True)
        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumTicketWorkPhase.DONE

    def test_with_stub_clients_completes(self) -> None:
        """Full pipeline with stub clients completes end-to-end."""
        stub_linear = StubLinearClient()
        stub_git = StubGitClient()
        handler = HandlerTicketWork(linear_client=stub_linear, git_client=stub_git)
        command = _make_command(autonomous=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumTicketWorkPhase.DONE
        assert state.pr_url == "https://github.com/org/repo/pull/42"
        assert "In Progress" in stub_linear.updated_states
        assert "In Review" in stub_linear.updated_states
