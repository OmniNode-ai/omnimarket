# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act-mode writer of the landing tick orchestrator onto the GitHub landing effect (OMN-23393, OMN-23405).

Golden: a recorded tick whose decision merges two PRs, run in an enabled act mode with the PRs'
node ids in the tick's own facts, ends with two arm commands on the GitHub landing effect's own
command topic, each pinned to its head.
Error: the same tick with the cutover set to another repository sends nothing and names why;
the same tick whose facts carry no node id sends nothing and names no_node_id; an act request
the overlay does not enable ends in the failed event with no write.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingAction,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_github_writer import (
    GithubEffectTickWriter,
    effect_command_topic,
    plan_write,
    pr_facts_of,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_write import (
    EnumTickWriteRefusal,
    ModelTickWriteOutcome,
)

pytestmark = pytest.mark.unit

REPLAY = Path(__file__).resolve().parents[2] / "fixtures" / "pr_landing_tick_replay"
EFFECT_TOPIC = "onex.cmd.omnimarket.pr-landing-github-requested.v1"
FIXED_ID = UUID("00000000-0000-4000-8000-000000000001")


def _snapshot(name: str) -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(
        (REPLAY / name).read_text(encoding="utf-8")
    )


def _actions(name: str) -> tuple[ModelLandingAction, ...]:
    return _snapshot(name).decision.actions


def _pr_facts(
    name: str, action: ModelLandingAction, **over: object
) -> ModelLandingPrFacts:
    """The recorded tick's own facts for the action's PR, with the write fields set."""
    recorded = pr_facts_of(_snapshot(name).facts).get(action.subject)
    assert recorded is not None, f"{action.subject} has no facts in {name}"
    fields: dict[str, object] = {"pr_node_id": "PR_node", **over}
    return recorded.model_copy(update=fields)


def _plan(name: str, cutover: str, **over: object) -> ModelTickWriteOutcome:
    action = _actions(name)[0]
    return plan_write(
        action,
        cutover_repository=cutover,
        pr_facts={action.subject: _pr_facts(name, action, **over)},
        mode=EnumPrLandingGithubMode.ENFORCE,
        correlation_id=FIXED_ID,
    )


def test_the_writer_publishes_on_the_effect_contract_topic() -> None:
    assert effect_command_topic() == EFFECT_TOPIC


def test_a_merge_is_an_arm_pinned_to_its_head() -> None:
    action = _actions("scenario_s22_green_disarmed_hold_release.json")[0]
    outcome = _plan("scenario_s22_green_disarmed_hold_release.json", "acme/infra")
    request = outcome.request
    assert request is not None
    assert outcome.refusal is None
    assert request.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert request.head_sha == action.head_sha
    assert request.pr_number == int(action.subject.partition("#")[2])
    assert request.pr_node_id == "PR_node"


def test_a_merge_on_a_merge_queue_base_is_an_enqueue() -> None:
    request = _plan(
        "scenario_s22_green_disarmed_hold_release.json", "acme/infra", merge_queue=True
    ).request
    assert request is not None
    assert request.operation is EnumPrLandingGithubOperation.ENQUEUE


def test_a_rerun_names_the_head_failed_runs() -> None:
    request = _plan(
        "scenario_s25_shared_red_changed_verdict.json",
        "acme/app",
        failed_run_ids=(7, 3, 7),
    ).request
    assert request is not None
    assert request.operation is EnumPrLandingGithubOperation.RERUN_RUNS
    assert request.run_ids == (3, 7)


def test_an_update_branch_is_an_update_at_the_head() -> None:
    action = _actions("scenario_s21_stale_cancelled_blocked_refresh.json")[0]
    request = _plan(
        "scenario_s21_stale_cancelled_blocked_refresh.json", "acme/app"
    ).request
    assert request is not None
    assert request.operation is EnumPrLandingGithubOperation.UPDATE_BRANCH
    assert request.head_sha == action.head_sha


@pytest.mark.parametrize(
    ("name", "cutover", "over", "refusal"),
    [
        (
            "scenario_s22_green_disarmed_hold_release.json",
            "acme/app",
            {},
            EnumTickWriteRefusal.OUT_OF_CUTOVER,
        ),
        (
            "scenario_s07_fix_submitted_awaiting_ci.json",
            "acme/app",
            {},
            EnumTickWriteRefusal.NOT_A_GITHUB_WRITE,
        ),
        (
            "scenario_s22_green_disarmed_hold_release.json",
            "acme/infra",
            {"pr_node_id": None},
            EnumTickWriteRefusal.NO_NODE_ID,
        ),
        (
            "scenario_s25_shared_red_changed_verdict.json",
            "acme/app",
            {},
            EnumTickWriteRefusal.NO_RUN_IDS,
        ),
        (
            "scenario_s22_green_disarmed_hold_release.json",
            "acme/infra",
            {"head_sha": "f" * 40},
            EnumTickWriteRefusal.HEAD_MOVED,
        ),
    ],
)
def test_an_action_the_effect_must_not_send_is_refused_by_name(
    name: str, cutover: str, over: dict[str, object], refusal: EnumTickWriteRefusal
) -> None:
    outcome = _plan(name, cutover, **over)
    assert outcome.request is None
    assert outcome.refusal is refusal
    assert outcome.detail


def _with_node_ids(facts: ModelLandingFacts) -> ModelLandingFacts:
    """The recorded tick's facts as the snapshot effect reads them: each PR with its node id."""
    prs = tuple(
        pr.model_copy(update={"pr_node_id": f"PR_{i}"})
        for i, pr in enumerate(facts.prs)
    )
    return facts.model_copy(update={"prs": prs})


def _act(
    cutover: str, *, act_enabled: bool = True, node_ids: bool = True
) -> tuple[InMemoryTickPublisher, GithubEffectTickWriter, EnumTickStatus, int]:
    snap = _snapshot("scenario_s22_green_disarmed_hold_release.json")
    facts = _with_node_ids(snap.facts) if node_ids else snap.facts
    effect_bus = InMemoryTickPublisher()
    writer = GithubEffectTickWriter(
        publisher=effect_bus,
        cutover_repository=cutover,
        mode=EnumPrLandingGithubMode.DRY_RUN,
    )
    result = HandlerPrLandingTickOrchestrator(
        writer=writer, publisher=InMemoryTickPublisher(), act_enabled=act_enabled
    ).handle(
        ModelTickRequest(
            correlation_id=uuid4(),
            mode=EnumTickMode.ACT,
            facts=facts,
            recorded_decision=snap.decision,
            fair_share=snap.fair_share,
        )
    )
    return effect_bus, writer, result.status, result.writes_performed


def test_the_tick_facts_carry_the_node_id_and_failed_run_ids() -> None:
    """A PR's facts, as the snapshot effect writes them, load with both write fields."""
    snap = _snapshot("scenario_s22_green_disarmed_hold_release.json")
    raw = snap.facts.model_dump(mode="json")
    raw["prs"][0]["pr_node_id"] = "PR_kwDOabc"
    raw["prs"][0]["failed_run_ids"] = [11, 12]
    facts = ModelLandingFacts.model_validate(raw)
    assert facts.prs[0].pr_node_id == "PR_kwDOabc"
    assert facts.prs[0].failed_run_ids == (11, 12)
    assert facts.prs[1].pr_node_id is None
    assert facts.prs[1].failed_run_ids == ()


def test_golden_chain_an_act_tick_sends_each_merge_to_the_github_effect() -> None:
    effect_bus, writer, status, writes = _act("acme/infra")
    assert status is EnumTickStatus.COMPARED
    assert [t for t, _ in effect_bus.published] == [EFFECT_TOPIC, EFFECT_TOPIC]
    requests = [
        e for _, e in effect_bus.published if isinstance(e, ModelPrLandingGithubRequest)
    ]
    assert len(requests) == len(effect_bus.published)
    assert {r.pr_number for r in requests} == {1869, 1871}
    assert {r.pr_node_id for r in requests} == {"PR_0", "PR_1"}
    assert all(
        r.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE for r in requests
    )
    assert all(o.refusal is None for o in writer.outcomes)
    assert writes == 2


def test_error_chain_an_act_tick_outside_the_cutover_sends_nothing() -> None:
    effect_bus, writer, status, writes = _act("acme/app")
    assert status is EnumTickStatus.COMPARED
    assert effect_bus.published == []
    assert {o.refusal for o in writer.outcomes} == {EnumTickWriteRefusal.OUT_OF_CUTOVER}
    assert writes == 0


def test_error_chain_tick_facts_without_a_node_id_send_no_merge() -> None:
    effect_bus, writer, status, writes = _act("acme/infra", node_ids=False)
    assert status is EnumTickStatus.COMPARED
    assert effect_bus.published == []
    assert {o.refusal for o in writer.outcomes} == {EnumTickWriteRefusal.NO_NODE_ID}
    assert writes == 0


def test_error_chain_act_not_enabled_by_the_overlay_never_calls_the_writer() -> None:
    effect_bus, writer, status, writes = _act("acme/infra", act_enabled=False)
    assert status is EnumTickStatus.FAILED
    assert effect_bus.published == []
    assert writer.outcomes == []
    assert writes == 0


OVERLAY = {
    "backend": "inmemory",
    "act_enabled": True,
    "max_snapshots": 10,
    "floor_piece": 9,
}


def test_the_overlay_names_the_cutover_repository_and_mode_for_act() -> None:
    overlay = ModelTickOrchestratorOverlay.model_validate(
        {**OVERLAY, "cutover_repository": "acme/infra", "github_mode": "dry_run"}
    )
    assert overlay.cutover_repository == "acme/infra"
    assert overlay.github_mode is EnumPrLandingGithubMode.DRY_RUN


@pytest.mark.parametrize(
    "missing",
    [{"github_mode": "dry_run"}, {"cutover_repository": "acme/infra"}],
)
def test_error_chain_act_without_a_cutover_or_mode_is_refused_by_the_overlay(
    missing: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="act_enabled needs cutover_repository"):
        ModelTickOrchestratorOverlay.model_validate({**OVERLAY, **missing})
