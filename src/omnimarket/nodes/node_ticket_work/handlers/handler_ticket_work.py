# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketWork — contract-driven per-ticket execution.

Implements all 7 phases of the ticket-work workflow:
  INTAKE -> RESEARCH -> QUESTIONS -> SPEC -> IMPLEMENT -> REVIEW -> DONE

Each phase method returns (updated_contract, success, error_message).
The caller drives the FSM via advance(); this handler owns the phase logic.

External dependencies are injected via Protocol-based DI:
  - ProtocolLinearClient: Linear API (fetch issue, update description/state)
  - ProtocolGitClient:    Git operations (worktree, commit, push, PR)

Both protocols are optional — if None, the handler operates in dry-run mode
(state machine advances without real side effects, useful for unit tests).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from omnibase_core.enums.ticket.enum_ticket_workflow_phase import (
    EnumTicketWorkflowPhase,
)
from omnibase_core.models.ticket.model_ticket_workflow_state import (
    ModelTicketWorkflowState,
)
from omnibase_core.models.ticket.model_workflow_gate import ModelWorkflowGate
from omnibase_core.models.ticket.model_workflow_requirement import (
    ModelWorkflowRequirement,
)
from omnibase_core.models.ticket.model_workflow_verification import (
    ModelWorkflowVerification,
)
from omnibase_core.utils.util_ticket_workflow_persistence import (
    extract_workflow_state,
    persist_workflow_state_locally,
    update_description_with_workflow_state,
)

from omnimarket.nodes.node_ticket_work.models.model_ticket_work_command import (
    ModelTicketWorkCommand,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    TERMINAL_PHASES,
    EnumTicketWorkPhase,
    ModelTicketWorkCompletedEvent,
    ModelTicketWorkPhaseEvent,
    ModelTicketWorkState,
    next_phase,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_ticket_work.protocols.protocol_git_client import (
        ModelRunResult,
        ProtocolGitClient,
    )
    from omnimarket.nodes.node_ticket_work.protocols.protocol_linear_client import (
        ProtocolLinearClient,
    )

_log = logging.getLogger(__name__)

_CANONICAL_WORKTREES = "/Volumes/PRO-G40/Code/omni_worktrees"  # local-path-ok

_DEFAULT_VERIFICATION_STEPS: list[ModelWorkflowVerification] = [
    ModelWorkflowVerification(
        id="v1",
        title="Unit tests pass",
        kind="unit_tests",
        command="uv run pytest tests/",
        expected="exit 0",
        blocking=True,
        status="pending",
    ),
    ModelWorkflowVerification(
        id="v2",
        title="Lint passes",
        kind="lint",
        command="uv run ruff check .",
        expected="exit 0",
        blocking=True,
        status="pending",
    ),
    ModelWorkflowVerification(
        id="v3",
        title="Type check passes",
        kind="mypy",
        command="uv run mypy src/",
        expected="exit 0",
        blocking=False,
        status="pending",
    ),
]

_DEFAULT_GATES: list[ModelWorkflowGate] = [
    ModelWorkflowGate(
        id="g1",
        title="Human approval",
        kind="human_approval",
        required=True,
        status="pending",
    )
]


class HandlerTicketWork:
    """Contract-driven per-ticket execution handler.

    Phases:
      INTAKE     — Fetch ticket from Linear, create/load contract
      RESEARCH   — Populate context (relevant files, patterns, notes)
      QUESTIONS  — Surface blockers; skip if no unanswered questions
      SPEC       — Generate requirements + verification steps + gates
      IMPLEMENT  — Create worktree, run implementation, run tests
      REVIEW     — Run pre-commit, push, create PR, update Linear status
      DONE       — Mark contract complete

    In autonomous mode, human gates are auto-approved.
    """

    def __init__(
        self,
        linear_client: ProtocolLinearClient | None = None,
        git_client: ProtocolGitClient | None = None,
    ) -> None:
        self._linear = linear_client
        self._git = git_client

    # ------------------------------------------------------------------
    # FSM core
    # ------------------------------------------------------------------

    def start(self, command: ModelTicketWorkCommand) -> ModelTicketWorkState:
        """Initialize FSM state from a start command."""
        return ModelTicketWorkState(
            correlation_id=command.correlation_id,
            current_phase=EnumTicketWorkPhase.IDLE,
            ticket_id=command.ticket_id,
            autonomous=command.autonomous,
            dry_run=command.dry_run,
        )

    def advance(
        self,
        state: ModelTicketWorkState,
        phase_success: bool,
        error_message: str | None = None,
        pr_url: str | None = None,
        commits: list[str] | None = None,
    ) -> tuple[ModelTicketWorkState, ModelTicketWorkPhaseEvent]:
        """Advance the FSM by one phase."""
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumTicketWorkPhase.FAILED
                err = (
                    error_message
                    or f"Circuit breaker: {new_failures} consecutive failures"
                )
                new_state = state.model_copy(
                    update={
                        "current_phase": to_phase,
                        "consecutive_failures": new_failures,
                        "error_message": err,
                    }
                )
            else:
                to_phase = from_phase
                new_state = state.model_copy(
                    update={
                        "consecutive_failures": new_failures,
                        "error_message": error_message,
                    }
                )
            event = ModelTicketWorkPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase)
        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
        }
        if pr_url is not None:
            updates["pr_url"] = pr_url
        if commits is not None:
            updates["commits"] = commits

        new_state = state.model_copy(update=updates)
        event = ModelTicketWorkPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
        )
        return new_state, event

    def make_completed_event(
        self, state: ModelTicketWorkState
    ) -> ModelTicketWorkCompletedEvent:
        """Create a completion event from the final state."""
        return ModelTicketWorkCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            ticket_id=state.ticket_id,
            pr_url=state.pr_url,
            error_message=state.error_message,
        )

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def run_intake(
        self,
        ticket_id: str,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """INTAKE: Fetch ticket from Linear, create or resume contract.

        Returns (contract, success, error_message).
        Auto-advances to research phase (no human gate needed).
        """
        if self._linear is None or dry_run:
            _log.info("[intake] dry-run: creating stub contract for %s", ticket_id)
            contract = ModelTicketWorkflowState(
                ticket_id=ticket_id,
                title=f"[dry-run] {ticket_id}",
                repo="unknown",
                phase=EnumTicketWorkflowPhase.RESEARCH,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
            return contract, True, None

        try:
            issue = self._linear.get_issue(ticket_id)
        except Exception as exc:
            return ModelTicketWorkflowState(), False, f"Linear fetch failed: {exc}"

        if issue is None:
            return (
                ModelTicketWorkflowState(),
                False,
                f"Linear issue not found: {ticket_id}",
            )

        existing = extract_workflow_state(issue.description)
        if existing is not None:
            _log.info(
                "[intake] resuming contract for %s (phase=%s)",
                ticket_id,
                existing.phase,
            )
            contract = existing
        else:
            _log.info("[intake] creating new contract for %s", ticket_id)
            # NOTE: `repo` is a repo-slug identifier (e.g. "omnimarket"), not a
            # display string. ModelLinearIssue does not expose a repo identifier
            # today, so we leave it empty; it gets populated by the
            # orchestrating skill/agent when the worktree is materialised.
            # handler_ticket_work.run_implement() already falls through to
            # OMNI_HOME alone when contract.repo is empty (see L439-L444).
            contract = ModelTicketWorkflowState(
                ticket_id=ticket_id,
                title=issue.title,
                repo="",
                phase=EnumTicketWorkflowPhase.RESEARCH,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )

        try:
            updated_desc = update_description_with_workflow_state(
                issue.description, contract
            )
            self._linear.update_issue_description(ticket_id, updated_desc)
            _persist_locally_safe(ticket_id, contract)
        except Exception as exc:
            _log.warning("[intake] persistence warning: %s", exc)

        return contract, True, None

    def run_research(
        self,
        contract: ModelTicketWorkflowState,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """RESEARCH: Advance phase to questions.

        Actual file exploration is performed by the orchestrating skill/agent layer.
        The handler advances the phase and persists context that was set externally.
        In dry-run mode, a placeholder context is injected so the FSM can advance.
        """
        if dry_run or self._linear is None:
            updated = contract.model_copy(
                update={
                    "phase": EnumTicketWorkflowPhase.QUESTIONS,
                    "updated_at": _now_iso(),
                    "context": contract.context.model_copy(
                        update={
                            "relevant_files": ["src/placeholder.py"],
                            "notes": "dry-run: research phase skipped",
                        }
                    ),
                }
            )
            return updated, True, None

        if not contract.context.relevant_files:
            _log.warning(
                "[research] no relevant_files populated; proceeding with empty context"
            )

        updated = contract.model_copy(
            update={
                "phase": EnumTicketWorkflowPhase.QUESTIONS,
                "updated_at": _now_iso(),
            }
        )
        _save_contract_safe(self._linear, contract.ticket_id, updated)
        return updated, True, None

    def run_questions(
        self,
        contract: ModelTicketWorkflowState,
        autonomous: bool = False,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """QUESTIONS: Block for unanswered questions, or auto-advance.

        Returns success=False (stay in phase) if required questions are unanswered
        and not in autonomous mode. Returns success=True to advance to spec.
        """
        unanswered = [
            q
            for q in contract.questions
            if q.required and not (q.answer and q.answer.strip())
        ]

        if unanswered and not autonomous:
            _log.info(
                "[questions] %d unanswered required questions — blocking for human gate",
                len(unanswered),
            )
            return (
                contract,
                False,
                f"{len(unanswered)} unanswered required questions pending",
            )

        if unanswered and autonomous:
            _log.info(
                "[questions] autonomous: skipping %d unanswered questions",
                len(unanswered),
            )

        updated = contract.model_copy(
            update={"phase": EnumTicketWorkflowPhase.SPEC, "updated_at": _now_iso()}
        )
        if self._linear and not dry_run:
            _save_contract_safe(self._linear, contract.ticket_id, updated)
        return updated, True, None

    def run_spec(
        self,
        contract: ModelTicketWorkflowState,
        autonomous: bool = False,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """SPEC: Inject default requirements/verification/gates if not already set.

        If the orchestrating layer has already populated these fields, they are
        preserved. Otherwise, sensible defaults are injected so the FSM can proceed.
        """
        updates: dict[str, object] = {
            "phase": EnumTicketWorkflowPhase.IMPLEMENT,
            "updated_at": _now_iso(),
        }

        if not contract.requirements:
            _log.info("[spec] injecting stub requirement for %s", contract.ticket_id)
            updates["requirements"] = [
                ModelWorkflowRequirement(
                    id="r1",
                    statement=f"Implement {contract.ticket_id}: {contract.title}",
                    rationale="Auto-generated from ticket title",
                    acceptance=["Implementation complete", "Tests pass"],
                )
            ]

        if not contract.verification:
            updates["verification"] = list(_DEFAULT_VERIFICATION_STEPS)

        if not contract.gates:
            if autonomous:
                updates["gates"] = [
                    g.model_copy(
                        update={"status": "approved", "resolved_at": _now_iso()}
                    )
                    for g in _DEFAULT_GATES
                ]
            else:
                updates["gates"] = list(_DEFAULT_GATES)

        updated = contract.model_copy(update=updates)
        if self._linear and not dry_run:
            _save_contract_safe(self._linear, contract.ticket_id, updated)
        return updated, True, None

    def run_implement(
        self,
        contract: ModelTicketWorkflowState,
        ticket_id: str,
        branch_name: str,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """IMPLEMENT: Create worktree, install pre-commit, run baseline tests.

        The actual code writing is performed by the orchestrating agent layer.
        This phase sets up the git environment and records the branch in the contract.
        """
        if self._git is None or dry_run:
            _log.info("[implement] dry-run: stub branch/commit for %s", ticket_id)
            updated = contract.model_copy(
                update={
                    "branch": branch_name or f"jonah/{ticket_id.lower()}-stub",
                    "commits": ["dry-run-sha"],
                    "phase": EnumTicketWorkflowPhase.REVIEW,
                    "updated_at": _now_iso(),
                }
            )
            return updated, True, None

        omni_home = os.environ.get("OMNI_HOME", "")
        repo_path = (
            os.path.join(omni_home, contract.repo)
            if (omni_home and contract.repo)
            else omni_home
        )

        try:
            wt = self._git.create_or_checkout_worktree(
                repo_path=repo_path,
                ticket_id=ticket_id,
                branch_name=branch_name,
            )
        except Exception as exc:
            return contract, False, f"Worktree creation failed: {exc}"

        try:
            self._git.install_pre_commit(wt.path)
        except Exception as exc:
            _log.warning("[implement] pre-commit install warning: %s", exc)

        test_result = self._git.run_tests(wt.path)
        if not test_result.success:
            _log.warning(
                "[implement] baseline tests failed (exit %d): %s",
                test_result.exit_code,
                test_result.stderr[:200],
            )

        updated = contract.model_copy(
            update={
                "branch": wt.branch,
                "phase": EnumTicketWorkflowPhase.REVIEW,
                "updated_at": _now_iso(),
            }
        )
        if self._linear:
            _save_contract_safe(self._linear, ticket_id, updated)
        return updated, True, None

    def run_review(
        self,
        contract: ModelTicketWorkflowState,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """REVIEW: Run pre-commit, push branch, create PR, update Linear status.

        Records verification results in the contract.
        Sets pr_url and advances to done.
        """
        ticket_id = contract.ticket_id
        branch = contract.branch or ""

        if self._git is None or dry_run:
            _log.info("[review] dry-run: stub PR for %s", ticket_id)
            updated = _mark_all_verification_passed(contract)
            updated = updated.model_copy(
                update={
                    "pr_url": "https://github.com/example/repo/pull/0",
                    "phase": EnumTicketWorkflowPhase.DONE,
                    "updated_at": _now_iso(),
                }
            )
            return updated, True, None

        wt_path = os.path.join(_CANONICAL_WORKTREES, ticket_id, contract.repo or "")

        pre_commit_result = self._git.run_pre_commit(wt_path)
        if not pre_commit_result.success:
            _log.warning(
                "[review] pre-commit failed (exit %d)", pre_commit_result.exit_code
            )
            return (
                contract,
                False,
                f"pre-commit failed: {pre_commit_result.stderr[:200]}",
            )

        test_result = self._git.run_tests(wt_path)
        verification = _record_verification_result(
            contract.verification, "v1", test_result
        )

        push_result = self._git.push_branch(wt_path, branch)
        if not push_result.success:
            return contract, False, f"git push failed: {push_result.stderr[:200]}"

        pr_body = _build_pr_body(contract)
        pr_result = self._git.create_pr(
            wt_path,
            title=f"{ticket_id}: {contract.title}",
            body=pr_body,
        )
        if not pr_result.success:
            return contract, False, f"PR creation failed: {pr_result.stderr[:200]}"

        pr_url = pr_result.stdout.strip()

        if self._linear:
            ok = self._linear.update_issue_state(ticket_id, "In Review")
            if not ok:
                _log.warning(
                    "[review] could not set Linear status to 'In Review' for %s",
                    ticket_id,
                )

        updated = contract.model_copy(
            update={
                "verification": verification,
                "pr_url": pr_url,
                "phase": EnumTicketWorkflowPhase.DONE,
                "updated_at": _now_iso(),
            }
        )
        if self._linear:
            _save_contract_safe(self._linear, ticket_id, updated)
        return updated, True, None

    def run_done(
        self,
        contract: ModelTicketWorkflowState,
        dry_run: bool = False,
    ) -> tuple[ModelTicketWorkflowState, bool, str | None]:
        """DONE: Mark contract phase as done and persist."""
        _log.info("[done] %s complete — PR: %s", contract.ticket_id, contract.pr_url)
        updated = contract.model_copy(
            update={"phase": EnumTicketWorkflowPhase.DONE, "updated_at": _now_iso()}
        )
        if self._linear and not dry_run:
            _save_contract_safe(self._linear, contract.ticket_id, updated)
        return updated, True, None

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run_full_pipeline(
        self,
        command: ModelTicketWorkCommand,
        phase_results: dict[EnumTicketWorkPhase, bool] | None = None,
    ) -> tuple[
        ModelTicketWorkState,
        list[ModelTicketWorkPhaseEvent],
        ModelTicketWorkCompletedEvent,
    ]:
        """Run the complete pipeline through all phases.

        When phase_results is provided, it overrides real execution (test mode).
        When not provided with real clients injected, each phase runs real I/O.
        When not provided without real clients, runs in structural dry-run mode.
        """
        state = self.start(command)
        events: list[ModelTicketWorkPhaseEvent] = []

        contract: ModelTicketWorkflowState | None = None
        branch_name = ""

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(state.current_phase)

            # Test override mode: use provided results, no real I/O
            if phase_results is not None:
                success = phase_results.get(target, True)
                error_msg = None if success else f"Phase {target.value} failed"
                state, event = self.advance(
                    state, phase_success=success, error_message=error_msg
                )
                events.append(event)
                if not success and state.current_phase not in TERMINAL_PHASES:
                    break
                continue

            # Real execution mode
            success, error_msg = True, None
            pr_url: str | None = None
            commits: list[str] | None = None

            if target == EnumTicketWorkPhase.INTAKE:
                contract, success, error_msg = self.run_intake(
                    command.ticket_id, dry_run=command.dry_run
                )

            elif target == EnumTicketWorkPhase.RESEARCH:
                if contract is not None:
                    contract, success, error_msg = self.run_research(
                        contract, dry_run=command.dry_run
                    )

            elif target == EnumTicketWorkPhase.QUESTIONS:
                if contract is not None:
                    contract, success, error_msg = self.run_questions(
                        contract, autonomous=command.autonomous, dry_run=command.dry_run
                    )

            elif target == EnumTicketWorkPhase.SPEC:
                if contract is not None:
                    contract, success, error_msg = self.run_spec(
                        contract, autonomous=command.autonomous, dry_run=command.dry_run
                    )
                    # Spec->implement transition: get branch name, update Linear to In Progress
                    if success and self._linear and not command.dry_run:
                        try:
                            issue = self._linear.get_issue(command.ticket_id)
                            branch_name = issue.branch_name
                            self._linear.update_issue_state(
                                command.ticket_id, "In Progress"
                            )
                            if contract is not None:
                                contract = contract.model_copy(
                                    update={"branch": branch_name}
                                )
                        except Exception as exc:
                            _log.warning(
                                "[spec] transition to In Progress failed: %s", exc
                            )

            elif target == EnumTicketWorkPhase.IMPLEMENT:
                if contract is not None:
                    contract, success, error_msg = self.run_implement(
                        contract,
                        ticket_id=command.ticket_id,
                        branch_name=branch_name,
                        dry_run=command.dry_run,
                    )
                    if success and contract is not None:
                        commits = list(contract.commits)

            elif target == EnumTicketWorkPhase.REVIEW:
                if contract is not None:
                    contract, success, error_msg = self.run_review(
                        contract, dry_run=command.dry_run
                    )
                    if success and contract is not None:
                        pr_url = contract.pr_url
                        commits = list(contract.commits)

            elif target == EnumTicketWorkPhase.DONE and contract is not None:
                contract, success, error_msg = self.run_done(
                    contract, dry_run=command.dry_run
                )

            state, event = self.advance(
                state,
                phase_success=success,
                error_message=error_msg,
                pr_url=pr_url,
                commits=commits,
            )
            events.append(event)

            if not success and state.current_phase not in TERMINAL_PHASES:
                break

        completed = self.make_completed_event(state)
        return state, events, completed

    def handle(
        self,
        command: ModelTicketWorkCommand,
        phase_results: dict[EnumTicketWorkPhase, bool] | None = None,
    ) -> tuple[
        ModelTicketWorkState,
        list[ModelTicketWorkPhaseEvent],
        ModelTicketWorkCompletedEvent,
    ]:
        """Primary entry point — delegates to run_full_pipeline."""
        return self.run_full_pipeline(command, phase_results=phase_results)


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _save_contract_safe(
    linear: ProtocolLinearClient,
    ticket_id: str,
    contract: ModelTicketWorkflowState,
) -> None:
    """Save contract to Linear and persist locally (best-effort, never raises)."""
    try:
        issue = linear.get_issue(ticket_id)
        if issue is None:
            _log.warning("[save_contract] ticket %s not found in Linear", ticket_id)
        else:
            updated_desc = update_description_with_workflow_state(
                issue.description, contract
            )
            linear.update_issue_description(ticket_id, updated_desc)
    except Exception as exc:
        _log.warning("[save_contract] Linear update failed for %s: %s", ticket_id, exc)
    try:
        persist_workflow_state_locally(ticket_id, contract)
    except (PermissionError, OSError) as exc:
        _log.warning(
            "[save_contract] local persistence failed for %s: %s", ticket_id, exc
        )


def _persist_locally_safe(ticket_id: str, contract: ModelTicketWorkflowState) -> None:
    try:
        persist_workflow_state_locally(ticket_id, contract)
    except (PermissionError, OSError) as exc:
        _log.warning("[persist_locally] failed for %s: %s", ticket_id, exc)


def _mark_all_verification_passed(
    contract: ModelTicketWorkflowState,
) -> ModelTicketWorkflowState:
    """Mark all verification steps as passed (dry-run helper)."""
    updated_v = [
        v.model_copy(update={"status": "passed", "executed_at": _now_iso()})
        for v in contract.verification
    ]
    return contract.model_copy(update={"verification": updated_v})


def _record_verification_result(
    verification: list[ModelWorkflowVerification],
    step_id: str,
    result: ModelRunResult,
) -> list[ModelWorkflowVerification]:
    """Record the result of a verification step by ID."""
    updated = []
    for v in verification:
        if v.id == step_id:
            updated.append(
                v.model_copy(
                    update={
                        "status": "passed" if result.success else "failed",
                        "evidence": (result.stdout or result.stderr)[:500],
                        "executed_at": _now_iso(),
                    }
                )
            )
        else:
            updated.append(v)
    return updated


def _build_pr_body(contract: ModelTicketWorkflowState) -> str:
    """Build a PR description body from the contract."""
    lines = [
        f"## {contract.ticket_id}: {contract.title}",
        "",
        "### Requirements",
    ]
    for req in contract.requirements:
        lines.append(f"- {req.statement}")
        for ac in req.acceptance:
            lines.append(f"  - {ac}")
    lines += ["", "### Verification"]
    for v in contract.verification:
        icon = "+" if v.status == "passed" else "-" if v.status == "failed" else "?"
        lines.append(f"- [{icon}] {v.title} (`{v.command}`)")
    return "\n".join(lines)


__all__: list[str] = ["HandlerTicketWork"]
