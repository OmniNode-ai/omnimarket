# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the first recorded tick with the stuck-head shape of tick 8088 (OMN-23409).

Tick 8088 of 2026-11-09 left a green head unmerged: its merge was deferred by strict order with a
null first. The script kept only that tick's decision, not its facts, so the tick itself cannot be
replayed. The snapshot step records every tick's facts; this replays the first recorded tick with
the same shape (green heads merged by the decision on one base whose strict setting is unread)
from its recorded facts alone.

Golden: the recorded fixture is what the step records from its declared sources; replayed, the
decision merges both green heads at their heads, the strict plan reads the unread base as strict
and orders the first merge first with nothing held and no merge deferred, and the act-mode writer
arms each merge at its head with the node id the step filled.
Error / failing control: the same tick replayed from facts the step did not fill sends no arm and
names no_node_id for each merge.
"""

from __future__ import annotations

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.models.landing_decision import (
    ModelLandingDecision,
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingCi,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingSnapshotPublisher,
    InMemoryLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelLandingSnapshotRequest,
    ModelLandingTickRecord,
)
from omnimarket.nodes.node_pr_landing_strict_plan_compute.handlers.handler_pr_landing_strict_plan import (
    HandlerPrLandingStrictPlan,
    short_key,
)
from omnimarket.nodes.node_pr_landing_strict_plan_compute.models.model_landing_strict_plan import (
    ModelLandingStrictPlanRequest,
    ModelLandingStrictPlanResult,
    ModelStrictAction,
    ModelStrictLivePr,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_github_writer import (
    plan_write,
    pr_facts_of,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    NodePathDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_write import (
    EnumTickWriteRefusal,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    RECORDS,
    SECOND,
    facts_source,
    overlay,
    scenario_facts,
    write_source,
)

pytestmark = pytest.mark.unit

RECORDED = RECORDS / "recorded_ticks_first_stuck_head.jsonl"
CUTOVER = "acme/infra"
# The watcher's base read of the cutover repository's PRs; its strict setting is unread (null).
BASE = "dev"


def _recorded() -> tuple[ModelLandingTickRecord, ...]:
    return tuple(
        ModelLandingTickRecord.model_validate_json(line)
        for line in RECORDED.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _merges(facts: ModelLandingFacts) -> list[tuple[str, str | None]]:
    decision = NodePathDecider().decide(facts, True)
    return [
        (a.subject, a.head_sha)
        for a in decision.actions
        if a.kind is EnumLandingActionKind.MERGE
    ]


def _stuck_head_shape(record: ModelLandingTickRecord) -> bool:
    """Green heads the decision merges, at least two on one base, so strict order decides which is first."""
    green = {p.pr for p in record.facts.prs if p.ci is EnumLandingCi.GREEN}
    merges = [s for s, _ in _merges(record.facts) if s in green]
    repos = [s.partition("#")[0] for s in merges]
    return any(repos.count(r) > 1 for r in repos)


def _first_stuck_head() -> ModelLandingTickRecord:
    return next(r for r in _recorded() if _stuck_head_shape(r))


def test_the_recorded_fixture_is_what_the_step_records() -> None:
    recorder = InMemoryLandingTickRecorder()
    handler = HandlerPrLandingSnapshot(
        overlay=overlay(),
        facts_source=facts_source(),
        write_source=write_source(),
        recorder=recorder,
        publisher=InMemoryLandingSnapshotPublisher(),
    )
    for tick in (FIRST, SECOND):
        handler.handle(ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=tick))
    assert recorder.recorded() == _recorded()


def test_the_first_recorded_stuck_head_tick_is_the_first_tick() -> None:
    assert _first_stuck_head().tick == FIRST


def _strict_plan(
    record: ModelLandingTickRecord,
) -> tuple[ModelLandingDecision, ModelLandingStrictPlanResult]:
    decision = NodePathDecider().decide(record.facts, True)
    prs = pr_facts_of(record.facts)
    live = {
        short_key(p.pr): ModelStrictLivePr(
            head=p.head_sha, base=BASE, rollup="SUCCESS", merge_state="CLEAN"
        )
        for p in prs.values()
    }
    request = ModelLandingStrictPlanRequest(
        now=record.facts.observed_at,
        actions=tuple(
            ModelStrictAction(kind=a.kind.value, subject=a.subject)
            for a in decision.actions
        ),
        live=live,
        strict_of={},
    )
    return decision, HandlerPrLandingStrictPlan().handle(request)


def test_replay_merges_both_green_heads_at_their_heads() -> None:
    record = _first_stuck_head()
    heads = {p.pr: p.head_sha for p in record.facts.prs}
    merges = _merges(record.facts)
    assert [s for s, _ in merges] == ["acme/infra#1869", "acme/infra#1871"]
    assert all(head == heads[subject] for subject, head in merges)


def test_replay_the_unread_strict_base_orders_the_first_merge_first_and_defers_no_merge() -> (
    None
):
    decision, plan = _strict_plan(_first_stuck_head())
    merge_idx = [
        i
        for i, a in enumerate(decision.actions)
        if a.kind is EnumLandingActionKind.MERGE
    ]
    assert plan.bases_to_read == ("infra:dev",)
    assert plan.ordered["infra:dev"] == tuple(merge_idx)
    first = plan.order[0]
    assert decision.actions[first].subject == "acme/infra#1869"
    assert plan.held == {}
    assert not set(plan.deferred_updates) & set(merge_idx)


_Write = tuple[
    EnumPrLandingGithubOperation | None,
    EnumTickWriteRefusal | None,
    str | None,
    str | None,
]


def _writes(facts: ModelLandingFacts) -> list[_Write]:
    decision = NodePathDecider().decide(facts, True)
    prs = pr_facts_of(facts)
    out: list[_Write] = []
    for action in decision.actions:
        if action.kind is not EnumLandingActionKind.MERGE:
            continue
        outcome = plan_write(
            action,
            cutover_repository=CUTOVER,
            pr_facts=prs,
            mode=EnumPrLandingGithubMode.DRY_RUN,
            correlation_id=FIXED_ID,
        )
        request = outcome.request
        out.append(
            (
                None if request is None else request.operation,
                outcome.refusal,
                None if request is None else request.required_head_sha(),
                None if request is None else request.pr_node_id,
            )
        )
    return out


def test_replay_the_writer_arms_each_merge_with_the_filled_node_id() -> None:
    record = _first_stuck_head()
    heads = [h for _, h in _merges(record.facts)]
    assert _writes(record.facts) == [
        (EnumPrLandingGithubOperation.ARM_AUTO_MERGE, None, heads[0], "PR_kwDOn1869"),
        (EnumPrLandingGithubOperation.ARM_AUTO_MERGE, None, heads[1], "PR_kwDOn1871"),
    ]


def test_failing_control_the_unfilled_tick_sends_no_arm_and_names_no_node_id() -> None:
    unfilled = scenario_facts(FIRST)
    assert _writes(unfilled) == [
        (None, EnumTickWriteRefusal.NO_NODE_ID, None, None),
        (None, EnumTickWriteRefusal.NO_NODE_ID, None, None),
    ]
