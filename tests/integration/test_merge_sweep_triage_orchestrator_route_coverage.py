# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-route coverage for node_merge_sweep_triage_orchestrator (OMN-13674).

ORCHESTRATOR archetype — a fan-out orchestrator with no ``fsm`` block. Its
declared state space is the 14-row classification→action decision table
(handler_triage docstring) whose outputs are the seven command classes it fans
out across its contract ``publish_topics`` (plus SKIP / no-command). This suite
proves:

* every declared output command class is reached (AutoMergeArm, Rebase, CiRerun
  in both re-trigger modes, ThreadReply, ConflictHunk, CiFix, PrPolishStart);
* every SKIP gate produces the correct negative control (a known-bad /
  non-actionable fixture emits NO command — the finding); and
* the orchestrator dispatches over the canonical in-memory bus
  (``integration_event_bus`` + ``LocalRuntimeBusAdapter``) and lands its terminal
  ``ModelHandlerOutput`` on the contract ``terminal_event`` topic.

The gh boundary (``_resolve_*`` async subprocess helpers) is replaced by
subclass override returning canned values — never a subprocess/asyncpg
monkeypatch. No real prod-mutating effect is executed.

Related: OMN-13674 (node state-coverage), OMN-8959 / OMN-8988 (decision table).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelClassifiedPR,
    ModelMergeSweepResult,
    ModelPRInfo,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage import (
    HandlerTriageOrchestrator,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
    ModelCiFixCommand,
    ModelCiRerunCommand,
    ModelConflictHunkCommand,
    ModelRebaseCommand,
    ModelThreadReplyCommand,
    ModelTriageRequest,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")
_REPO = "OmniNode-ai/omni_home"
_TERMINAL_TOPIC = "onex.evt.omnimarket.merge-sweep-state-reduced.v1"
_TRIAGE_TOPIC = "onex.cmd.omnimarket.merge-sweep-triage.v1"


class _StubResolveTriage(HandlerTriageOrchestrator):
    """Triage orchestrator with the gh/subprocess boundary replaced by canned data.

    Every ``_resolve_*`` helper is overridden to return a deterministic value so
    the decision table's routing logic runs without any real gh CLI call. This is
    constructor/subclass injection at the I/O boundary — the decision logic under
    test is the real handler code.
    """

    async def _resolve_pr_graphql_id(
        self, repo: str, pr_number: int
    ) -> tuple[str | None, str | None]:
        return f"PR_kw{pr_number}", f"feat/{pr_number}"

    async def _resolve_pr_refs(
        self, repo: str, pr_number: int
    ) -> tuple[str, str, str] | None:
        return f"feat/{pr_number}", "main", f"sha{pr_number}"

    async def _resolve_failing_run_id(self, repo: str, pr_number: int) -> str | None:
        return f"run{pr_number}"

    async def _resolve_failing_job_name(self, repo: str, pr_number: int) -> str | None:
        return f"job-{pr_number}"

    async def _resolve_open_thread_comment_ids(
        self, repo: str, pr_number: int
    ) -> list[str]:
        return [f"RT_{pr_number}"]

    async def _resolve_conflict_files(self, repo: str, pr_number: int) -> list[str]:
        return [f"src/file_{pr_number}.py"]

    async def _resolve_event_delivery_gap(
        self, repo: str, pr_number: int, base_branch: str | None = None
    ) -> tuple[tuple[str, ...], str, str]:
        return ("Runtime Sweep",), f"feat/{pr_number}", f"sha{pr_number}"


def _pr(
    number: int,
    *,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    review_decision: str | None = "APPROVED",
    required_checks_pass: bool = True,
    required_checks_failed: bool = False,
    is_draft: bool = False,
    required_approving_review_count: int | None = None,
) -> ModelPRInfo:
    return ModelPRInfo(
        number=number,
        title=f"PR {number}",
        repo=_REPO,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        required_checks_pass=required_checks_pass,
        required_checks_failed=required_checks_failed,
        is_draft=is_draft,
        required_approving_review_count=required_approving_review_count,
    )


def _classified(pr: ModelPRInfo, track: EnumPRTrack) -> ModelClassifiedPR:
    return ModelClassifiedPR(pr=pr, track=track, reason="test")


def _request(
    classified: list[ModelClassifiedPR],
    *,
    emit_pr_polish_commands: bool = False,
    dry_run: bool = True,
) -> ModelTriageRequest:
    return ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=classified),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
        emit_pr_polish_commands=emit_pr_polish_commands,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Positive routes: every declared output command class is reached.
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pr", "track", "expected_type"),
    [
        pytest.param(
            _pr(201, merge_state_status="CLEAN", review_decision="APPROVED"),
            EnumPRTrack.A_UPDATE,
            ModelAutoMergeArmCommand,
            id="rule2-auto-merge-arm",
        ),
        pytest.param(
            _pr(202, merge_state_status="BEHIND", review_decision="APPROVED"),
            EnumPRTrack.A_UPDATE,
            ModelRebaseCommand,
            id="rule3-track-a-rebase",
        ),
        pytest.param(
            _pr(203),
            EnumPRTrack.A_RESOLVE,
            ModelThreadReplyCommand,
            id="rule5-thread-reply",
        ),
        pytest.param(
            _pr(
                204,
                merge_state_status="BLOCKED",
                required_checks_pass=False,
                required_checks_failed=True,
            ),
            EnumPRTrack.B_POLISH,
            ModelCiRerunCommand,
            id="rule6-ci-rerun",
        ),
        pytest.param(
            _pr(205, mergeable="CONFLICTING", merge_state_status="DIRTY"),
            EnumPRTrack.B_POLISH,
            ModelConflictHunkCommand,
            id="rule7-conflict-hunk",
        ),
        pytest.param(
            _pr(206, mergeable="MERGEABLE", merge_state_status="DIRTY"),
            EnumPRTrack.B_POLISH,
            ModelCiFixCommand,
            id="rule9-ci-fix",
        ),
        pytest.param(
            _pr(
                207,
                merge_state_status="BEHIND",
                required_checks_pass=False,
                required_checks_failed=True,
            ),
            EnumPRTrack.B_POLISH,
            ModelRebaseCommand,
            id="rule8-track-b-rebase",
        ),
    ],
)
async def test_route_emits_declared_command(
    pr: ModelPRInfo,
    track: EnumPRTrack,
    expected_type: type,
) -> None:
    """Each decision-table row reaches its declared output command class."""
    handler = _StubResolveTriage()
    output = await handler.handle(_request([_classified(pr, track)]))

    assert len(output.events) == 1, (
        f"expected exactly one command, got {[type(e).__name__ for e in output.events]}"
    )
    cmd = output.events[0]
    assert isinstance(cmd, expected_type)
    assert cmd.pr_number == pr.number  # every declared command class carries pr_number
    # ORCHESTRATOR must never return a typed result payload.
    assert output.result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rule6b_blocked_no_failing_run_emits_empty_commit_rerun() -> None:
    """Rule 6b: BLOCKED + no terminal failure + required-context gap → empty_commit rerun."""
    pr = _pr(
        208,
        merge_state_status="BLOCKED",
        required_checks_pass=True,
        required_checks_failed=False,
    )
    handler = _StubResolveTriage()
    output = await handler.handle(_request([_classified(pr, EnumPRTrack.B_POLISH)]))

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelCiRerunCommand)
    assert cmd.retrigger_mode == "empty_commit"
    assert cmd.run_id_github == ""
    assert cmd.missing_required_contexts == ("Runtime Sweep",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_track_b_emits_pr_polish_start_command() -> None:
    """emit_pr_polish_commands=True fans out a PrPolishStart alongside the remediation."""
    pr = _pr(
        209,
        merge_state_status="BLOCKED",
        required_checks_pass=False,
        required_checks_failed=True,
    )
    handler = _StubResolveTriage()
    output = await handler.handle(
        _request(
            [_classified(pr, EnumPRTrack.B_POLISH)],
            emit_pr_polish_commands=True,
            dry_run=False,
        )
    )

    polish = [e for e in output.events if isinstance(e, ModelPrPolishStartCommand)]
    assert len(polish) == 1
    assert polish[0].pr_number == 209
    assert polish[0].dry_run is False
    # The specialized remediation command is still emitted too.
    assert any(isinstance(e, ModelCiRerunCommand) for e in output.events)


# ---------------------------------------------------------------------------
# Negative controls: known-bad / non-actionable fixtures emit NO command.
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pr", "track"),
    [
        pytest.param(_pr(301, is_draft=True), EnumPRTrack.A_UPDATE, id="rule1-draft"),
        pytest.param(
            _pr(302, review_decision="CHANGES_REQUESTED"),
            EnumPRTrack.A_UPDATE,
            id="rule13-changes-requested",
        ),
        pytest.param(
            _pr(303, mergeable="UNKNOWN"),
            EnumPRTrack.A_UPDATE,
            id="rule11-mergeable-unknown",
        ),
        pytest.param(
            _pr(304, merge_state_status="UNKNOWN"),
            EnumPRTrack.A_UPDATE,
            id="rule12-merge-state-unknown",
        ),
        pytest.param(_pr(305), EnumPRTrack.SKIP, id="rule10-skip-track"),
        pytest.param(
            _pr(
                306,
                merge_state_status="BEHIND",
                review_decision=None,
                required_approving_review_count=1,
            ),
            EnumPRTrack.A_UPDATE,
            id="rule4-behind-needs-review",
        ),
    ],
)
async def test_skip_gate_emits_no_command(pr: ModelPRInfo, track: EnumPRTrack) -> None:
    """A known-bad / non-actionable PR must produce no command (SKIP)."""
    handler = _StubResolveTriage()
    output = await handler.handle(_request([_classified(pr, track)]))
    assert len(output.events) == 0, (
        f"expected SKIP (no command), got {[type(e).__name__ for e in output.events]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resolve_no_open_threads_skips() -> None:
    """A_RESOLVE with zero open thread comment IDs is a SKIP (negative control)."""

    class _NoThreads(_StubResolveTriage):
        async def _resolve_open_thread_comment_ids(
            self, repo: str, pr_number: int
        ) -> list[str]:
            return []

    handler = _NoThreads()
    output = await handler.handle(
        _request([_classified(_pr(307), EnumPRTrack.A_RESOLVE)])
    )
    assert len(output.events) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_id_resolution_failure_skips() -> None:
    """Rule 2 fixture but GraphQL node-id resolution fails → SKIP (no command)."""

    class _FailResolve(_StubResolveTriage):
        async def _resolve_pr_graphql_id(
            self, repo: str, pr_number: int
        ) -> tuple[str | None, str | None]:
            return None, None

    pr = _pr(308, merge_state_status="CLEAN", review_decision="APPROVED")
    handler = _FailResolve()
    output = await handler.handle(_request([_classified(pr, EnumPRTrack.A_UPDATE)]))
    assert len(output.events) == 0


# ---------------------------------------------------------------------------
# Bus round-trip: the orchestrator dispatches over the canonical in-memory bus
# and lands its terminal output on the contract terminal_event topic.
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_bus_round_trip_lands_terminal_output(
    integration_event_bus: Any,
) -> None:
    """subscribe(triage) → publish classified list → terminal ModelHandlerOutput published."""
    await integration_event_bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=_StubResolveTriage(),
            handler_name="merge-sweep-triage-orchestrator",
            input_model_cls=ModelTriageRequest,
            output_topic=_TERMINAL_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _TRIAGE_TOPIC,
            on_message=adapter.on_message,
            group_id="omn-13674-triage-round-trip",
        )

        pr = _pr(401, merge_state_status="CLEAN", review_decision="APPROVED")
        request = ModelTriageRequest(
            classification=ModelMergeSweepResult(
                classified=[_classified(pr, EnumPRTrack.A_UPDATE)]
            ),
            run_id=uuid4(),
            correlation_id=uuid4(),
            emit_pr_polish_commands=False,
        )
        await integration_event_bus.publish(
            _TRIAGE_TOPIC,
            key=None,
            value=request.model_dump_json().encode("utf-8"),
        )

        history = await integration_event_bus.get_event_history(topic=_TERMINAL_TOPIC)
        assert len(history) == 1, "expected one terminal ModelHandlerOutput on the bus"
        payload = json.loads(history[0].value)
        # The orchestrator fanned out exactly one auto-merge-arm command.
        assert len(payload["events"]) == 1
        assert payload["result"] is None
    finally:
        await integration_event_bus.close()
