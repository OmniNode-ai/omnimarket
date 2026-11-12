# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex scm rerun-request` through the landing tick orchestrator on an in-memory bus (OMN-23830).

Seam: the verb (scm_rerun_request) and the orchestrator share the request and answer models and the topics of the
orchestrator contract's rerun_request section. The fake bus is the shape of test_ticket_cli.py's: the command is read
off the command topic, run through the real orchestrator handler on the recorded tick 725 facts, and the answer goes
back on the completed or the failed topic as the bus publisher sends it, with no envelope.
Golden: a failed job on the current head is answered rerun on the completed topic, exit 0.
Error chain: a limit already reached and a job that is not failed are answered on the failed topic, exit 1.
Control: with a raised limit the same second request is answered rerun, so the refusal comes from the overlay's limit.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.scm_rerun_request import (
    load_rerun_request_topics,
    rerun_request_command,
)
from omnimarket.models.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
    rerun_topics,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "tick_725_omnimarket.json"
)
TARGET = "OmniNode-ai/omnimarket#3293"
SNAP = ModelTickSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8"))
PR_FACTS = next(p for p in SNAP.facts.prs if p.pr == TARGET)
HEAD = PR_FACTS.head_sha
JOBS = PR_FACTS.red_checks


@dataclass
class Message:
    value: bytes
    partition: int = 0
    offset: str = "7"


class TickBus:
    """Answers each command the way the unit does: through the orchestrator handler, un-enveloped."""

    def __init__(self, limit: int | None) -> None:
        self.callbacks: dict[str, object] = {}
        self.commands: list[ModelLandingRerunRequest] = []
        # one handler for the bus's life: its ledger is what makes the second request a duplicate
        self.publisher = InMemoryTickPublisher()
        self.handler = HandlerPrLandingTickOrchestrator(
            publisher=self.publisher, rerun_limit=limit
        )

    async def subscribe(
        self, topic, node_identity=None, on_message=None, *, group_id=None
    ):
        raise AssertionError(f"rerun-request joined a consumer group on {topic}")

    async def subscribe_ungrouped(self, topic, on_message):
        self.callbacks[topic] = on_message

        async def unsubscribe() -> None:
            del self.callbacks[topic]

        return unsubscribe

    async def publish(self, topic, key, value, headers=None):
        topics = load_rerun_request_topics()
        assert topic == topics.command
        assert topics.success in self.callbacks
        assert topics.failure in self.callbacks
        envelope = ModelEventEnvelope[dict].model_validate_json(value)
        command = ModelLandingRerunRequest.model_validate(envelope.payload)
        self.commands.append(command)
        before = len(self.publisher.published)
        self.handler.handle(
            ModelTickRequest(
                correlation_id=command.correlation_id,
                mode=EnumTickMode.OBSERVE,
                facts=SNAP.facts,
                recorded_decision=SNAP.decision,
                fair_share=SNAP.fair_share,
                rerun_requests=(command,),
            )
        )
        for event_topic, event in self.publisher.published[before:]:
            callback = self.callbacks.get(event_topic)
            if callback is not None:
                await callback(Message(event.model_dump_json().encode()))


def _invoke(monkeypatch: pytest.MonkeyPatch, bus: TickBus, job: str, head: str = HEAD):
    @contextlib.asynccontextmanager
    async def opened(_opts):
        yield bus

    command = rerun_request_command(opened, lambda *_: head)
    return CliRunner().invoke(
        command,
        [
            TARGET,
            "--job",
            job,
            "--reason",
            "runner outage",
            "--lane",
            "lane-a",
            "--head-sha",
            head,
            "--bus",
            "inmemory",
        ],
    )


def test_the_verb_is_served_and_the_rerun_entry_is_no_longer_refused() -> None:
    assert "rerun-request" in scm_cli.scm_group.commands
    refused = {r.verb for r in scm_cli.SCM_REFUSED_VERBS}
    assert "rerun" not in refused
    assert {"run-rerun", "pr-rerun"} <= refused  # pr-rerun stays refused for others


def test_the_verb_and_the_orchestrator_read_the_same_topics() -> None:
    topics = load_rerun_request_topics()
    assert (topics.success, topics.failure) == rerun_topics()


def test_golden_chain_a_failed_job_is_answered_rerun_on_the_completed_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = TickBus(limit=2)
    result = _invoke(monkeypatch, bus, JOBS[0])
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["failed"] is False
    assert answer["receipt"]["terminal_topic"] == rerun_topics()[0]
    assert answer["payload"]["verdict"] == "rerun"
    assert answer["payload"]["requester"] == "lane-a"
    assert answer["payload"]["reason"] == "runner outage"
    assert bus.commands[0].pr == TARGET


def test_error_chain_the_limit_reached_is_answered_on_the_failed_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = TickBus(limit=1)
    assert _invoke(monkeypatch, bus, JOBS[0]).exit_code == 0
    refused = _invoke(monkeypatch, bus, JOBS[1])
    assert refused.exit_code == 1, refused.output
    answer = json.loads(refused.output)
    assert answer["failed"] is True
    assert answer["receipt"]["terminal_topic"] == rerun_topics()[1]
    assert answer["payload"]["verdict"] == "limit_reached"
    assert answer["payload"]["limit"] == 1
    control = TickBus(limit=2)  # the failing control: only the overlay's limit differs
    assert _invoke(monkeypatch, control, JOBS[0]).exit_code == 0
    assert (
        json.loads(_invoke(monkeypatch, control, JOBS[1]).output)["payload"]["verdict"]
        == "rerun"
    )


def test_error_chain_a_job_that_is_not_failed_is_answered_on_the_failed_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke(monkeypatch, TickBus(limit=2), "a job that is green")
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["payload"]["verdict"] == "job_not_failed"
