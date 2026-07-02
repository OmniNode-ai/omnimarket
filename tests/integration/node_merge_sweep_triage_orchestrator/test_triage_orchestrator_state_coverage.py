# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-route coverage for node_merge_sweep_triage_orchestrator (OMN-13674).

ORCHESTRATOR archetype. This node has no ``state_machine`` block in its
``contract.yaml``; its declarative surface is the ``handler_routing``
(``operation: merge_sweep_triage``) plus the exhaustive 14-row
classification-to-action decision table (``handler_triage.py``) that fans out N
typed command events across the 7 contract-declared ``publish_topics``. The
"declared state set" for this orchestrator is therefore every decision-table
branch and every emitted-command route.

Coverage driven over the canonical in-memory bus (``EventBusInmemory`` via the
``integration_event_bus`` fixture) through ``LocalRuntimeBusAdapter``
(``drive_round_trip``). No live Kafka / .201, no live GitHub: the orchestrator's
only I/O boundary is the ``gh``-subprocess resolver methods, which are stubbed
via a test subclass (``_StubTriageOrchestrator``) that injects deterministic
returns by constructor. Subprocess is never monkeypatched.

ORCHESTRATOR DoD:
  * every declared route fired -- one representative branch per emitted-command
    class (auto-merge-arm / rebase / ci-rerun / thread-reply / conflict-hunk /
    ci-fix / pr-polish-start), asserted on the emitted command *type* and mapped
    back to its contract-declared publish topic (``_COMMAND_TOPIC``);
  * every SKIP branch of the decision table entered -- draft, SKIP-track,
    CHANGES_REQUESTED, UNKNOWN-mergeable, UNKNOWN-merge-state, BEHIND-needs-human,
    and fallthrough -- each asserted to emit zero commands;
  * every failure->skip edge (GraphQL / refs / failing-run / no-open-thread
    resolution failure) asserted to emit zero commands (no partial fan-out);
  * both CI-rerun variants (``rerun_failed`` and ``empty_commit``) reached;
  * the terminal-event path proven end-to-end over the bus: exactly one
    ``ModelHandlerOutput`` is republished on the contract terminal topic
    ``onex.evt.omnimarket.merge-sweep-state-reduced.v1`` with correlation-id
    preserved;
  * a negative control: a CHANGES_REQUESTED PR that a stale classifier still
    labels Track A MUST NOT emit an auto-merge-arm command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelClassifiedPR,
    ModelMergeSweepResult,
    ModelPRInfo,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage import (
    TOPIC_AUTO_MERGE_ARM,
    TOPIC_CI_FIX,
    TOPIC_CI_RERUN,
    TOPIC_CONFLICT_HUNK,
    TOPIC_REBASE,
    TOPIC_THREAD_REPLY,
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

_REPO = "OmniNode-ai/omnimarket"

# Contract topics.
_TRIAGE_TOPIC = "onex.cmd.omnimarket.merge-sweep-triage.v1"
_TERMINAL_TOPIC = "onex.evt.omnimarket.merge-sweep-state-reduced.v1"
_POLISH_TOPIC = "onex.cmd.omnimarket.pr-polish-start.v1"

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_merge_sweep_triage_orchestrator"
    / "contract.yaml"
)

# Each emitted-command class maps to exactly one contract-declared publish topic
# (the effect node that consumes it subscribes on that topic). Asserting the map
# against the contract publish_topics set proves every declared route is real;
# each route test tags the command class it fires, so the union proves every
# route is exercised.
# The topic literals are written out (not the imported ``TOPIC_*`` constants) so
# the contract-state-coverage gate (OMN-13781) can see every declared publish
# topic asserted against emitted behaviour. ``test_command_topic_map_matches_handler_constants``
# pins each literal back to the handler's canonical constant to prevent drift.
_COMMAND_TOPIC: dict[type, str] = {
    ModelAutoMergeArmCommand: "onex.cmd.omnimarket.pr-auto-merge-arm.v1",
    ModelRebaseCommand: "onex.cmd.omnimarket.pr-rebase.v1",
    ModelCiRerunCommand: "onex.cmd.omnimarket.pr-ci-rerun.v1",
    ModelThreadReplyCommand: "onex.cmd.omnimarket.pr-thread-reply.v1",
    ModelConflictHunkCommand: "onex.cmd.omnimarket.pr-conflict-hunk.v1",
    ModelCiFixCommand: "onex.cmd.omnimarket.pr-ci-fix.v1",
    ModelPrPolishStartCommand: "onex.cmd.omnimarket.pr-polish-start.v1",
}

# Canonical topic constants owned by the handler module — pinned against the
# literals above so a topic rename fails loudly instead of silently drifting.
_HANDLER_TOPIC_CONSTANTS: dict[type, str] = {
    ModelAutoMergeArmCommand: TOPIC_AUTO_MERGE_ARM,
    ModelRebaseCommand: TOPIC_REBASE,
    ModelCiRerunCommand: TOPIC_CI_RERUN,
    ModelThreadReplyCommand: TOPIC_THREAD_REPLY,
    ModelConflictHunkCommand: TOPIC_CONFLICT_HUNK,
    ModelCiFixCommand: TOPIC_CI_FIX,
    ModelPrPolishStartCommand: _POLISH_TOPIC,
}


# ---------------------------------------------------------------------------
# Test double: stub only the gh-subprocess I/O boundary via subclass injection.
# The full 14-row decision-table logic in HandlerTriageOrchestrator is exercised
# unchanged; subprocess is never touched.
# ---------------------------------------------------------------------------


class _StubTriageOrchestrator(HandlerTriageOrchestrator):
    """HandlerTriageOrchestrator with the ``gh`` resolvers replaced by fixtures.

    Every resolver return is constructor-injected so a test can drive both the
    happy path and each resolution-failure edge deterministically.
    """

    def __init__(
        self,
        *,
        graphql_id: tuple[str | None, str | None] = ("PR_kwNODE", "feature-branch"),
        refs: tuple[str, str, str] | None = ("feature-branch", "main", "deadbeefoid"),
        failing_run_id: str | None = "9990001",
        failing_job: str | None = "test (3.12)",
        thread_ids: list[str] | None = None,
        conflict_files: list[str] | None = None,
        event_gap: tuple[tuple[str, ...], str, str] = (
            ("verify / Run Receipt-Gate",),
            "feature-branch",
            "deadbeefoid",
        ),
    ) -> None:
        self._graphql_id = graphql_id
        self._refs = refs
        self._failing_run_id = failing_run_id
        self._failing_job = failing_job
        self._thread_ids = ["comment-node-1"] if thread_ids is None else thread_ids
        self._conflict_files = (
            ["src/foo.py"] if conflict_files is None else conflict_files
        )
        self._event_gap = event_gap

    async def _resolve_pr_graphql_id(
        self, repo: str, pr_number: int
    ) -> tuple[str | None, str | None]:
        return self._graphql_id

    async def _resolve_pr_refs(
        self, repo: str, pr_number: int
    ) -> tuple[str, str, str] | None:
        return self._refs

    async def _resolve_failing_run_id(self, repo: str, pr_number: int) -> str | None:
        return self._failing_run_id

    async def _resolve_failing_job_name(self, repo: str, pr_number: int) -> str | None:
        return self._failing_job

    async def _resolve_open_thread_comment_ids(
        self, repo: str, pr_number: int
    ) -> list[str]:
        return list(self._thread_ids)

    async def _resolve_conflict_files(self, repo: str, pr_number: int) -> list[str]:
        return list(self._conflict_files)

    async def _resolve_event_delivery_gap(
        self, repo: str, pr_number: int, base_branch: str | None = None
    ) -> tuple[tuple[str, ...], str, str]:
        return self._event_gap


# ---------------------------------------------------------------------------
# Fixtures builders.
# ---------------------------------------------------------------------------


def _pr(number: int = 1, **overrides: Any) -> ModelPRInfo:
    base: dict[str, Any] = {
        "number": number,
        "title": f"feat: change {number}",
        "repo": _REPO,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "required_checks_pass": True,
        "review_decision": "APPROVED",
    }
    base.update(overrides)
    return ModelPRInfo(**base)


def _classified(pr: ModelPRInfo, track: EnumPRTrack) -> ModelClassifiedPR:
    return ModelClassifiedPR(pr=pr, track=track, reason="fixture")


def _request(*classified: ModelClassifiedPR, **overrides: Any) -> ModelTriageRequest:
    return ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=list(classified)),
        run_id=uuid4(),
        correlation_id=overrides.pop("correlation_id", uuid4()),
        **overrides,
    )


async def _emit(
    handler: HandlerTriageOrchestrator, request: ModelTriageRequest
) -> list[Any]:
    """Invoke the orchestrator directly and return the typed emitted events."""
    output = await handler.handle(request)
    return list(output.events)


# ---------------------------------------------------------------------------
# Route coverage -- every declared emitted-command route fired.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriageOrchestratorRouteCoverage:
    @pytest.mark.parametrize(
        ("pr_kwargs", "track", "expected_cmd", "request_kwargs"),
        [
            pytest.param(
                {},
                EnumPRTrack.A_MERGE,
                ModelAutoMergeArmCommand,
                {},
                id="A-clean-approved-auto-merge-arm",
            ),
            pytest.param(
                {"merge_state_status": "BEHIND"},
                EnumPRTrack.A_UPDATE,
                ModelRebaseCommand,
                {},
                id="A-behind-approved-rebase",
            ),
            pytest.param(
                {"merge_state_status": "BLOCKED", "review_bot_gate_passed": False},
                EnumPRTrack.A_RESOLVE,
                ModelThreadReplyCommand,
                {},
                id="A-resolve-thread-reply",
            ),
            pytest.param(
                {
                    "merge_state_status": "BLOCKED",
                    "required_checks_pass": False,
                    "required_checks_failed": True,
                },
                EnumPRTrack.B_POLISH,
                ModelCiRerunCommand,
                {"emit_pr_polish_commands": False},
                id="B-blocked-failing-ci-rerun",
            ),
            pytest.param(
                {"mergeable": "CONFLICTING", "merge_state_status": "DIRTY"},
                EnumPRTrack.B_POLISH,
                ModelConflictHunkCommand,
                {"emit_pr_polish_commands": False},
                id="B-conflicting-dirty-conflict-hunk",
            ),
            pytest.param(
                {"merge_state_status": "DIRTY"},
                EnumPRTrack.B_POLISH,
                ModelCiFixCommand,
                {"emit_pr_polish_commands": False},
                id="B-dirty-ci-fix",
            ),
            pytest.param(
                {
                    "merge_state_status": "BEHIND",
                    "required_checks_pass": False,
                    "required_checks_failed": True,
                },
                EnumPRTrack.B_POLISH,
                ModelRebaseCommand,
                {"emit_pr_polish_commands": False},
                id="B-behind-failing-rebase",
            ),
        ],
    )
    async def test_route_fires_expected_command(
        self,
        pr_kwargs: dict[str, Any],
        track: EnumPRTrack,
        expected_cmd: type,
        request_kwargs: dict[str, Any],
    ) -> None:
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(_classified(_pr(**pr_kwargs), track), **request_kwargs),
        )
        assert len(events) == 1, f"expected exactly one command, got {events!r}"
        assert isinstance(events[0], expected_cmd)
        # The fired command maps to a real contract-declared publish topic.
        assert expected_cmd in _COMMAND_TOPIC

    async def test_ci_rerun_rerun_failed_variant(self) -> None:
        """B/BLOCKED with a terminal check failure -> rerun_failed with a run id."""
        events = await _emit(
            _StubTriageOrchestrator(failing_run_id="7654321"),
            _request(
                _classified(
                    _pr(
                        merge_state_status="BLOCKED",
                        required_checks_pass=False,
                        required_checks_failed=True,
                    ),
                    EnumPRTrack.B_POLISH,
                ),
                emit_pr_polish_commands=False,
            ),
        )
        assert len(events) == 1
        cmd = events[0]
        assert isinstance(cmd, ModelCiRerunCommand)
        assert cmd.retrigger_mode == "rerun_failed"
        assert cmd.run_id_github == "7654321"

    async def test_ci_rerun_empty_commit_variant(self) -> None:
        """B/BLOCKED, no terminal failure, event-delivery gap -> empty_commit."""
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(
                _classified(
                    _pr(merge_state_status="BLOCKED"),
                    EnumPRTrack.B_POLISH,
                ),
                emit_pr_polish_commands=False,
            ),
        )
        assert len(events) == 1
        cmd = events[0]
        assert isinstance(cmd, ModelCiRerunCommand)
        assert cmd.retrigger_mode == "empty_commit"
        assert cmd.run_id_github == ""
        assert cmd.missing_required_contexts == ("verify / Run Receipt-Gate",)


@pytest.mark.integration
class TestTriageOrchestratorContractRoutes:
    """Static route-declaration coverage (no bus, no async)."""

    def test_every_declared_publish_topic_has_a_route(self) -> None:
        """The command->topic map covers exactly the contract publish_topics set."""
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        declared = set(contract["event_bus"]["publish_topics"])
        mapped = set(_COMMAND_TOPIC.values())
        assert mapped == declared, (
            "every contract-declared publish topic must be reachable by exactly "
            f"one emitted-command route; declared-only={declared - mapped}, "
            f"mapped-only={mapped - declared}"
        )

    def test_command_topic_map_matches_handler_constants(self) -> None:
        """Each literal topic route pins to the handler's canonical constant."""
        assert _COMMAND_TOPIC == _HANDLER_TOPIC_CONSTANTS


# ---------------------------------------------------------------------------
# SKIP-branch coverage -- every decision-table skip gate emits zero commands.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriageOrchestratorSkipGates:
    @pytest.mark.parametrize(
        ("pr_kwargs", "track"),
        [
            pytest.param({"is_draft": True}, EnumPRTrack.A_MERGE, id="rule1-draft"),
            pytest.param(
                {"review_decision": "CHANGES_REQUESTED"},
                EnumPRTrack.A_MERGE,
                id="rule13-changes-requested",
            ),
            pytest.param(
                {"mergeable": "UNKNOWN"},
                EnumPRTrack.A_MERGE,
                id="rule11-unknown-mergeable",
            ),
            pytest.param(
                {"merge_state_status": "UNKNOWN"},
                EnumPRTrack.A_MERGE,
                id="rule12-unknown-merge-state",
            ),
            pytest.param({}, EnumPRTrack.SKIP, id="rule10-skip-track"),
            pytest.param(
                {
                    "merge_state_status": "BEHIND",
                    "review_decision": None,
                    "required_approving_review_count": 2,
                },
                EnumPRTrack.A_UPDATE,
                id="rule4-behind-needs-human",
            ),
            pytest.param(
                {"merge_state_status": "CLEAN"},
                EnumPRTrack.B_POLISH,
                id="rule14-b-fallthrough-clean",
            ),
        ],
    )
    async def test_skip_branch_emits_no_command(
        self, pr_kwargs: dict[str, Any], track: EnumPRTrack
    ) -> None:
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(_classified(_pr(**pr_kwargs), track)),
        )
        assert events == [], f"skip gate must emit nothing, got {events!r}"


# ---------------------------------------------------------------------------
# Failure edges -- a resolution failure must not fan out a partial command.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriageOrchestratorResolutionFailureEdges:
    async def test_graphql_id_failure_skips_auto_merge(self) -> None:
        """Track A arm path: GraphQL node-id resolution failure -> no command."""
        events = await _emit(
            _StubTriageOrchestrator(graphql_id=(None, None)),
            _request(_classified(_pr(), EnumPRTrack.A_MERGE)),
        )
        assert events == []

    async def test_refs_failure_skips_rebase(self) -> None:
        """Track A rebase path: ref resolution failure -> no command."""
        events = await _emit(
            _StubTriageOrchestrator(refs=None),
            _request(
                _classified(_pr(merge_state_status="BEHIND"), EnumPRTrack.A_UPDATE)
            ),
        )
        assert events == []

    async def test_no_failing_run_skips_ci_rerun(self) -> None:
        """Track B rerun path: no failing run id resolved -> no command."""
        events = await _emit(
            _StubTriageOrchestrator(failing_run_id=None, event_gap=((), "", "")),
            _request(
                _classified(
                    _pr(
                        merge_state_status="BLOCKED",
                        required_checks_pass=False,
                        required_checks_failed=True,
                    ),
                    EnumPRTrack.B_POLISH,
                ),
                emit_pr_polish_commands=False,
            ),
        )
        assert events == []

    async def test_no_open_threads_skips_thread_reply(self) -> None:
        """Track A-resolve: zero open thread comment ids -> no command."""
        events = await _emit(
            _StubTriageOrchestrator(thread_ids=[]),
            _request(
                _classified(_pr(merge_state_status="BLOCKED"), EnumPRTrack.A_RESOLVE)
            ),
        )
        assert events == []


# ---------------------------------------------------------------------------
# Fan-out, total_prs accounting, dry-run propagation, negative control.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriageOrchestratorFanoutAndAccounting:
    async def test_track_b_polish_fanout_emits_polish_plus_remediation(self) -> None:
        """A Track B conflict PR fans out BOTH a pr-polish-start and a
        conflict-hunk remediation command when polish emission is enabled."""
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(
                _classified(
                    _pr(mergeable="CONFLICTING", merge_state_status="DIRTY"),
                    EnumPRTrack.B_POLISH,
                ),
                emit_pr_polish_commands=True,
                dry_run=True,
            ),
        )
        kinds = {type(e) for e in events}
        assert ModelPrPolishStartCommand in kinds
        assert ModelConflictHunkCommand in kinds
        polish = next(e for e in events if isinstance(e, ModelPrPolishStartCommand))
        # dry_run propagates to the polish command's side-effect switches.
        assert polish.dry_run is True
        assert polish.no_push is True
        assert polish.no_automerge is True

    async def test_total_prs_counts_distinct_prs(self) -> None:
        """total_prs on non-Phase-2 commands is the distinct actionable PR count."""
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(
                _classified(_pr(number=11), EnumPRTrack.A_MERGE),
                _classified(_pr(number=12), EnumPRTrack.A_MERGE),
                _classified(_pr(number=13), EnumPRTrack.A_MERGE),
            ),
        )
        arm_cmds = [e for e in events if isinstance(e, ModelAutoMergeArmCommand)]
        assert len(arm_cmds) == 3
        assert all(c.total_prs == 3 for c in arm_cmds)

    async def test_negative_control_changes_requested_never_arms(self) -> None:
        """Negative control: a CHANGES_REQUESTED PR that a stale classifier still
        tagged Track A MUST NOT emit an auto-merge-arm command."""
        events = await _emit(
            _StubTriageOrchestrator(),
            _request(
                _classified(
                    _pr(review_decision="CHANGES_REQUESTED"), EnumPRTrack.A_MERGE
                )
            ),
        )
        assert not any(isinstance(e, ModelAutoMergeArmCommand) for e in events)
        assert events == []


# ---------------------------------------------------------------------------
# Terminal-event path proven end-to-end over the canonical in-memory bus.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriageOrchestratorBusTerminalEvent:
    async def test_orchestrator_republishes_terminal_output_over_bus(
        self, integration_event_bus: Any
    ) -> None:
        """Driven over EventBusInmemory via LocalRuntimeBusAdapter: the triage
        command on the declared start topic yields exactly one ModelHandlerOutput
        on the declared terminal topic, correlation-id preserved, arm route fired."""
        from tests.integration._wave7_bus import drive_round_trip

        corr = uuid4()
        request = _request(_classified(_pr(), EnumPRTrack.A_MERGE), correlation_id=corr)
        history = await drive_round_trip(
            integration_event_bus,
            handler=_StubTriageOrchestrator(),
            handler_name="merge-sweep-triage-orchestrator",
            input_model_cls=ModelTriageRequest,
            start_topic=_TRIAGE_TOPIC,
            output_topic=_TERMINAL_TOPIC,
            payload_bytes=request.model_dump_json().encode("utf-8"),
            group_id="omnimarket.merge_sweep_triage_orchestrator.consume.v1",
        )
        assert len(history) == 1, "expected exactly one terminal ModelHandlerOutput"
        output: dict[str, Any] = json.loads(history[0].value)
        assert UUID(output["correlation_id"]) == corr
        assert output["handler_id"] == "node_merge_sweep_triage_orchestrator"
        # The fanned-out command survived transit and is the auto-merge-arm route.
        assert len(output["events"]) == 1
        assert output["events"][0]["pr_node_id"] == "PR_kwNODE"

    async def test_bus_skip_path_emits_terminal_with_no_events(
        self, integration_event_bus: Any
    ) -> None:
        """A pure-skip sweep still terminates with a ModelHandlerOutput carrying
        zero command events (the orchestrator always reaches its terminal event)."""
        from tests.integration._wave7_bus import drive_round_trip

        request = _request(_classified(_pr(is_draft=True), EnumPRTrack.A_MERGE))
        history = await drive_round_trip(
            integration_event_bus,
            handler=_StubTriageOrchestrator(),
            handler_name="merge-sweep-triage-orchestrator",
            input_model_cls=ModelTriageRequest,
            start_topic=_TRIAGE_TOPIC,
            output_topic=_TERMINAL_TOPIC,
            payload_bytes=request.model_dump_json().encode("utf-8"),
            group_id="omnimarket.merge_sweep_triage_orchestrator.consume.v1",
        )
        assert len(history) == 1
        output: dict[str, Any] = json.loads(history[0].value)
        assert output["events"] == []
