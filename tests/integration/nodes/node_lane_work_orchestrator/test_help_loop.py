# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help loop end to end on the in-memory bus (OMN-26722, plan 3.5, 3.6 and 12.3).

Task 6's orchestrator, the classifier, the reducer and this task's help handler, with a stub
harness answering the pinned delegation. This is the seam that failed in spike 2: nothing called
the help handler, so no ``delegate-skill.v1`` request went out.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.model_harness_backend_call import ModelHarnessBackendCallResult
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_lane_work_orchestrator.event_topics import (
    ROLE_HELP_REQUESTED,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_help import (
    HandlerLaneWorkHelp,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_intake import (
    HandlerLaneWorkIntake,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_help_requested import (
    ModelLaneHelpRequested,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_fold_result import (
    ModelLaneWorkFoldResult,
)
from tests.unit.nodes.node_lane_work_orchestrator.rig_lane_work import T0, Rig

pytestmark = pytest.mark.integration


class _CrashError(Exception):
    """A process crash injected between two writes."""


class MemoryPorts:
    """The handler's ledger and delegation topics, in memory.

    The ledger deduplicates by request id (``idempotent: true``); the delegation topic is read
    back by action id, as the handler's recovery does on the broker.
    """

    def __init__(self) -> None:
        self.ledger: dict[UUID, str] = {}
        self.ledger_calls = 0
        self.delegations: list[ModelDelegateSkillRequest] = []
        self.crash_after_ledger = False
        self.crash_after_delegation = False

    def append_ledger(self, request: ModelWorkLedgerAppendRequest) -> None:
        self.ledger_calls += 1
        self.ledger.setdefault(request.request_id, request.rows)

    def send_delegation(self, request: ModelDelegateSkillRequest) -> None:
        self.delegations.append(request)

    def delegations_sent(self) -> frozenset[str]:
        return frozenset(str(r.metadata["action_id"]) for r in self.delegations)


def _answer_for(request: ModelDelegateSkillRequest, content: str, *, ok: bool = True):  # type: ignore[no-untyped-def]
    meta = request.metadata
    return ModelHarnessBackendCallResult.model_validate(
        {
            "request_id": str(request.correlation_id),
            "status": "completed" if ok else "refused",
            "ok": ok,
            "content": content,
            "backend_id": request.backend_id,
            "model_name": meta["model"],
            "attempts": [
                {
                    "request_id": str(request.correlation_id),
                    "attempt": 1,
                    "tier": meta["help_tier"],
                    "backend_id": request.backend_id,
                    "harness": meta["harness"],
                    "model_name": meta["model"],
                    "wall_seconds": 2.0,
                    "exit_cause": "completed" if ok else "process_failed",
                    "accepted": ok,
                }
            ],
        }
    )


class Loop:
    """Routes the fold's help messages to the handler and the stub harness's answers back."""

    def __init__(self, rig: Rig, ports: MemoryPorts) -> None:
        self.rig = rig
        self.ports = ports
        self.handler = HandlerLaneWorkHelp(ports)
        self.intake = HandlerLaneWorkIntake(rig.orchestrator)
        self.help_messages: list[ModelLaneHelpRequested] = []

    def deliver(self, result: ModelLaneWorkFoldResult) -> None:
        for output in result.published:
            if output.role != ROLE_HELP_REQUESTED:
                continue
            message = ModelLaneHelpRequested.model_validate(output.payload)
            self.help_messages.append(message)
            dispatched = self.handler.handle(message)
            if dispatched.ingress is not None:
                self.deliver(self.rig.orchestrator.handle(dispatched.ingress))

    def answer(self, content: str, *, ok: bool = True) -> ModelLaneWorkFoldResult:
        request = self.ports.delegations[-1]
        ingress = self.intake.handle(_answer_for(request, content, ok=ok))
        assert len(ingress) == 1
        result = self.rig.orchestrator.handle(ingress[0])
        self.deliver(result)
        return result


def _to_cause_unknown(rig: Rig, worker_tier: str) -> ModelLaneWorkFoldResult:
    rig.send("start", worker_tier=worker_tier)
    rig.send("worker_started", lane_name="lane-x", body_ticket_ids=("OMN-26722",))
    rig.send("claim_confirmed")
    rig.check("check_red", T0 + timedelta(minutes=5))
    rig.send("log_read", red_kind="other", evidence=("the log names no cause",))
    return rig.send("cause_unknown", evidence=("five whys ended",))


def test_cause_unknown_sends_one_pinned_request_one_ledger_row_and_the_answer_moves_the_workflow() -> (
    None
):
    rig, ports = Rig(), MemoryPorts()
    loop = Loop(rig, ports)
    loop.deliver(_to_cause_unknown(rig, "claude-medium"))
    assert rig.name == "help_requested"
    assert len(loop.help_messages) == 1
    assert len(ports.delegations) == 1
    sent = ports.delegations[0]
    assert sent.backend_id == "harness-claude-code-large"
    assert sent.wait is False
    assert len(ports.ledger) == 1
    assert "| MSG |" in next(iter(ports.ledger.values()))
    loop.answer(json.dumps({"kind": "help_cause_found", "cause": "the base is old"}))
    assert rig.name == "fixing"


def test_a_failed_top_tier_answer_ends_blocked_help_exhausted_with_the_question() -> (
    None
):
    rig, ports = Rig(), MemoryPorts()
    loop = Loop(rig, ports)
    loop.deliver(_to_cause_unknown(rig, "claude-medium"))
    loop.answer("not json at all")
    assert rig.name == "blocked"
    terminals = rig.orchestrator.journal.read("terminal")
    assert [t[1]["reason"] for t in terminals] == ["help_exhausted"]
    assert terminals[0][1]["decision_needed"]


def test_a_top_tier_worker_that_needs_help_ends_blocked_with_nothing_sent() -> None:
    rig, ports = Rig(), MemoryPorts()
    loop = Loop(rig, ports)
    loop.deliver(_to_cause_unknown(rig, "claude-large"))
    assert ports.delegations == []
    assert rig.name == "blocked"
    terminals = rig.orchestrator.journal.read("terminal")
    assert len(terminals) == 1


def _redeliver(loop: Loop) -> None:
    message = loop.help_messages[-1]
    loop.handler.handle(message)


def test_a_crash_between_the_ledger_write_and_the_delegation_leaves_one_of_each() -> (
    None
):
    rig, ports = Rig(), MemoryPorts()
    loop = Loop(rig, ports)
    result = _to_cause_unknown(rig, "claude-medium")
    message = ModelLaneHelpRequested.model_validate(
        next(o.payload for o in result.published if o.role == ROLE_HELP_REQUESTED)
    )
    loop.help_messages.append(message)

    class CrashingPorts(MemoryPorts):
        def send_delegation(self, request: ModelDelegateSkillRequest) -> None:
            raise _CrashError

    crashing = CrashingPorts()
    crashing.ledger = ports.ledger
    with pytest.raises(_CrashError):
        HandlerLaneWorkHelp(crashing).handle(message)
    assert len(ports.ledger) == 1
    assert ports.delegations == []
    _redeliver(loop)  # the recovered consumer gets the message again
    assert len(ports.ledger) == 1  # the append deduplicated by request id
    assert len(ports.delegations) == 1


def test_a_crash_after_both_writes_and_before_the_commit_sends_nothing_twice() -> None:
    rig, ports = Rig(), MemoryPorts()
    loop = Loop(rig, ports)
    loop.deliver(_to_cause_unknown(rig, "claude-medium"))
    assert len(ports.delegations) == 1
    _redeliver(loop)
    _redeliver(loop)
    assert len(ports.ledger) == 1
    assert len(ports.delegations) == 1
    assert ports.ledger_calls == 3  # re-sent, and deduplicated
