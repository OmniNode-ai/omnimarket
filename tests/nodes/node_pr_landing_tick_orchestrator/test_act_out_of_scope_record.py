# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act mode on a repository the old controller rules out of scope writes the record it needs (OMN-23458).

The landing snapshot step's records carry no recorded decision, and the old controller reads no facts and
decides nothing for a repository on the merge sweep's out_of_scope list (omnimemory, RULING
2026-11-10T01:41:29Z). Act mode for such a cutover repository refused every tick with
RECORDED_DECISION_MISSING. It now writes that repository's recorded decision, no action, and compares
that repository's subjects only. A cutover repository the old controller still decides for has no such
record to write and is refused, with the reason named. At the merge base the golden test fails: act mode
refuses RECORDED_DECISION_MISSING.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
    NodePathDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickFailureReason,
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "scenario_s22_green_disarmed_hold_release.json"
)
COMPARED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compared.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compare-failed.v1"


def _snapshot() -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _cutover() -> tuple[str, int]:
    """A repository the node path acts on in the fixture tick, and how many actions it gets."""
    snap = _snapshot()
    decision = NodePathDecider().decide(snap.facts, snap.fair_share)
    repos = [a.subject.partition("#")[0] for a in decision.actions if "#" in a.subject]
    assert repos, "the fixture tick holds no pull request action"
    return repos[0], repos.count(repos[0])


def _act_without_record() -> ModelTickRequest:
    snap = _snapshot()
    return ModelTickRequest(
        correlation_id=uuid4(),
        mode=EnumTickMode.ACT,
        facts=snap.facts,
        recorded_decision=None,
        fair_share=snap.fair_share,
    )


def _handler(
    out_of_scope: tuple[str, ...], cutover: str | None
) -> tuple[
    HandlerPrLandingTickOrchestrator,
    InMemoryTickWriter,
    InMemoryTickPublisher,
    InMemoryEmissionSink,
]:
    writer, publisher, sink = (
        InMemoryTickWriter(),
        InMemoryTickPublisher(),
        InMemoryEmissionSink(),
    )
    handler = HandlerPrLandingTickOrchestrator(
        writer=writer,
        publisher=publisher,
        act_enabled=True,
        emission_sink=sink,
        floor_piece=9,
        cutover_repository=cutover,
        script_out_of_scope=out_of_scope,
    )
    return handler, writer, publisher, sink


def test_golden_chain_act_writes_the_out_of_scope_record_and_compares_that_repository_only() -> (
    None
):
    repository, actions = _cutover()
    handler, writer, publisher, sink = _handler((repository,), repository)
    result = handler.handle(_act_without_record())
    assert result.status is EnumTickStatus.COMPARED, result.failure_detail
    assert result.recorded_written is True
    assert result.recorded_actions == 0
    assert result.node_actions == actions
    # every pull request row is the cutover repository's; the tick's own row stays
    assert {
        row.subject.partition("#")[0] for row in result.rows if row.subject != "<tick>"
    } == {repository}
    # only the cutover repository's actions reach the writer
    assert [a.subject.partition("#")[0] for a in writer.performed] == [
        repository
    ] * actions
    assert [topic for topic, _ in publisher.published] == [COMPARED_TOPIC]
    assert str(sink.events[0].phase).endswith("start")
    assert str(sink.events[-1].phase).endswith("success")
    assert sink.events[-1].counters["recorded_written"] == 1


def test_error_chain_act_for_a_repository_the_old_controller_decides_for_is_refused() -> (
    None
):
    repository, _ = _cutover()
    handler, writer, publisher, sink = _handler((), repository)
    result = handler.handle(_act_without_record())
    assert result.status is EnumTickStatus.FAILED
    assert result.failure_reason is EnumTickFailureReason.RECORDED_DECISION_MISSING
    assert repository in result.failure_detail
    assert "out of scope" in result.failure_detail
    assert writer.performed == []
    assert [topic for topic, _ in publisher.published] == [FAILED_TOPIC]
    assert str(sink.events[-1].phase).endswith("failure")
    assert sink.events[-1].failure_code == "recorded_decision_missing"


def test_control_observe_mode_never_writes_a_record() -> None:
    repository, _ = _cutover()
    handler, writer, _, _ = _handler((repository,), repository)
    request = _act_without_record().model_copy(update={"mode": EnumTickMode.OBSERVE})
    result = handler.handle(request)
    assert result.failure_reason is EnumTickFailureReason.RECORDED_DECISION_MISSING
    assert result.recorded_written is False
    assert writer.performed == []


def test_control_act_with_no_cutover_repository_is_refused_naming_it() -> None:
    repository, _ = _cutover()
    handler, _, _, _ = _handler((repository,), None)
    result = handler.handle(_act_without_record())
    assert result.failure_reason is EnumTickFailureReason.RECORDED_DECISION_MISSING
    assert "names no cutover repository" in result.failure_detail
