# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane's rerun request is a fact the landing tick reads and answers (OMN-23830).

Follows test_golden_chain_pr_landing_tick_orchestrator.py: the recorded tick 725 snapshot is the facts,
an in-memory publisher reads the completed and failed events. Control: with the limit raised the same
third request is granted, so the limit_reached verdict comes from the declared limit and not from the code.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.pr_landing_rerun_request.enum_rerun_verdict import (
    EnumRerunVerdict,
)
from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_answer import (
    ModelLandingRerunAnswer,
)
from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.models.landing_decision import EnumLandingActionKind
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
    rerun_topics,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = (
    REPO / "tests" / "fixtures" / "pr_landing_tick_replay" / "tick_725_omnimarket.json"
)
PR = "OmniNode-ai/omnimarket#3183"
HEAD = "3a1beb08992004c653336cbb4bf8039d20364b58"
OLD_HEAD = "0000000000000000000000000000000000000001"
RED_JOB = "occ-autobind / outcome"
LIMIT = 2


def _ask(
    job: str = RED_JOB, head: str = HEAD, pr: str = PR
) -> ModelLandingRerunRequest:
    return ModelLandingRerunRequest(
        correlation_id=uuid4(),
        pr=pr,
        head_sha=head,
        job=job,
        reason="the job failed on a runner outage",
        requester="f1-lane",
    )


def _tick(
    requests: tuple[ModelLandingRerunRequest, ...],
    mode: EnumTickMode = EnumTickMode.OBSERVE,
) -> ModelTickRequest:
    snap = ModelTickSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8"))
    return ModelTickRequest(
        correlation_id=uuid4(),
        mode=mode,
        facts=snap.facts,
        recorded_decision=snap.decision,
        fair_share=snap.fair_share,
        rerun_requests=requests,
    )


def _answers(
    requests: tuple[ModelLandingRerunRequest, ...], *, limit: int | None = LIMIT
) -> tuple[ModelLandingRerunAnswer, ...]:
    handler = HandlerPrLandingTickOrchestrator(rerun_limit=limit)
    return handler.handle(_tick(requests)).rerun_answers


def test_a_failed_job_on_the_current_head_is_rerun_once_then_a_duplicate() -> None:
    first, second = _answers((_ask(), _ask()))
    assert first.verdict is EnumRerunVerdict.RERUN
    assert first.would_rerun is True  # observe: recorded, nothing written
    assert (first.requester, first.reason) == (
        "f1-lane",
        "the job failed on a runner outage",
    )
    assert second.verdict is EnumRerunVerdict.DUPLICATE
    assert second.reruns_on_head == 1


def test_past_the_per_head_limit_from_the_overlay_is_limit_reached_and_control_grants_when_raised() -> (
    None
):
    red = "OmniNode-ai/omnimarket#3293"
    snap = ModelTickSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8"))
    pr = next(p for p in snap.facts.prs if p.pr == red)
    jobs = pr.red_checks[:3]
    asks = tuple(_ask(job, pr.head_sha, red) for job in jobs)
    capped = _answers(asks, limit=2)
    assert [a.verdict for a in capped] == [
        EnumRerunVerdict.RERUN,
        EnumRerunVerdict.RERUN,
        EnumRerunVerdict.LIMIT_REACHED,
    ]
    assert capped[2].limit == 2
    raised = _answers(asks, limit=3)  # the failing control: only the overlay differs
    assert [a.verdict for a in raised] == [EnumRerunVerdict.RERUN] * 3


def test_a_request_for_a_head_that_moved_is_head_moved() -> None:
    (answer,) = _answers((_ask(head=OLD_HEAD),))
    assert answer.verdict is EnumRerunVerdict.HEAD_MOVED
    assert answer.current_head_sha == HEAD


def test_a_job_that_is_not_failed_is_job_not_failed() -> None:
    (answer,) = _answers((_ask(job="a job that is green"),))
    assert answer.verdict is EnumRerunVerdict.JOB_NOT_FAILED


def test_no_declared_limit_refuses_every_request_it_cannot_bound() -> None:
    (answer,) = _answers((_ask(),), limit=None)
    assert answer.verdict is EnumRerunVerdict.LIMIT_REACHED
    assert answer.limit == 0


def test_golden_and_error_chain_events_go_to_the_contract_topics() -> None:
    completed, failed = rerun_topics()
    publisher = InMemoryTickPublisher()
    handler = HandlerPrLandingTickOrchestrator(publisher=publisher, rerun_limit=LIMIT)
    handler.handle(_tick((_ask(), _ask(head=OLD_HEAD), _ask(job="green job"))))
    rerun_events = [
        (topic, event)
        for topic, event in publisher.published
        if isinstance(event, ModelLandingRerunAnswer)
    ]
    by_topic = {
        verdict: topic for topic, event in rerun_events for verdict in (event.verdict,)
    }
    assert by_topic[EnumRerunVerdict.RERUN] == completed
    assert by_topic[EnumRerunVerdict.HEAD_MOVED] == failed
    assert by_topic[EnumRerunVerdict.JOB_NOT_FAILED] == failed


def test_act_mode_sends_one_rerun_through_the_writer_and_observe_sends_none() -> None:
    writer = InMemoryTickWriter()
    act = HandlerPrLandingTickOrchestrator(
        writer=writer, act_enabled=True, rerun_limit=LIMIT
    )
    result = act.handle(_tick((_ask(), _ask()), EnumTickMode.ACT))
    reruns = [a for a in writer.performed if a.kind is EnumLandingActionKind.RERUN]
    assert [a.subject for a in reruns if a.head_sha == HEAD and a.subject == PR] == [PR]
    assert result.rerun_answers[0].would_rerun is False
    observe_writer = InMemoryTickWriter()
    HandlerPrLandingTickOrchestrator(writer=observe_writer, rerun_limit=LIMIT).handle(
        _tick((_ask(),))
    )
    assert observe_writer.performed == []


def test_a_granted_rerun_survives_in_the_ledger_so_a_later_tick_answers_duplicate(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_rerun_ledger import (
        FileRerunLedger,
    )

    path = tmp_path / "rerun-ledger.jsonl"
    first = HandlerPrLandingTickOrchestrator(
        rerun_limit=LIMIT, rerun_ledger=FileRerunLedger(path)
    ).handle(_tick((_ask(),)))
    assert first.rerun_answers[0].verdict is EnumRerunVerdict.RERUN
    later = HandlerPrLandingTickOrchestrator(
        rerun_limit=LIMIT, rerun_ledger=FileRerunLedger(path)
    ).handle(_tick((_ask(),)))
    assert later.rerun_answers[0].verdict is EnumRerunVerdict.DUPLICATE
