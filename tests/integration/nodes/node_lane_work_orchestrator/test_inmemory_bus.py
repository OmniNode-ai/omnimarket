# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A crash between any two publishes loses no output and duplicates no receipt (OMN-26721 AC1).

A crash-free run of each scenario fixes the inputs and the number of publishes. Then each
publish position, in turn, is made to fail once: the process is restarted (a new
orchestrator rebuilt from what the broker holds, ``recover()``), the uncommitted input is
redelivered, and the rest of the run goes on. Every run must leave the same receipts,
commands and terminals as the crash-free run, each exactly once.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from omnibase_core.models.lane_work import ModelLaneWorkEvent

from omnimarket.nodes.node_lane_work_orchestrator.event_topics import topic_for
from omnimarket.nodes.node_lane_work_orchestrator.journal import InMemoryLaneWorkJournal
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_orchestrator.orchestration.lane_work_orchestrator import (
    LaneWorkOrchestrator,
)
from tests.unit.nodes.node_lane_work_orchestrator.rig_lane_work import (
    T0,
    Rig,
    make_orchestrator,
)

pytestmark = pytest.mark.integration

ROLES = (
    "transition",
    "step_completed",
    "terminal",
    "ledger_append",
    "ledger_query",
    "lab_proof",
    "repo_write",
    "handoff",
)


class CrashError(Exception):
    """The process died here."""


class CrashingJournal(InMemoryLaneWorkJournal):
    """Fails the ``fail_at``-th publish once, before the message reaches the broker."""

    def __init__(self, fail_at: int | None = None) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.count = 0
        self.crashed = False

    def publish(self, role: str, output_id: str, payload: dict[str, Any]) -> None:
        self.count += 1
        if not self.crashed and self.fail_at == self.count:
            self.crashed = True
            raise CrashError(f"publish {self.count} of {role}")
        super().publish(role, output_id, payload)


def _handed_off() -> Rig:
    rig = Rig()
    rig.to_handed_off()
    return rig


def _blocked() -> Rig:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    state = rig.orchestrator.state_of(rig.workflow_id or "")
    rig.send(
        "deadline_passed",
        deadline_id="worker",
        deadline_attempt=state.armed_deadlines["worker"],
    )
    return rig


SCENARIOS = {
    "handed_off": (_handed_off, "handed_off"),
    "blocked": (_blocked, "blocked"),
}


def _census(journal: InMemoryLaneWorkJournal) -> dict[str, Counter[str]]:
    return {role: Counter(oid for oid, _ in journal.read(role)) for role in ROLES}


def _expected_census(inputs: list[ModelLaneWorkIngress]) -> dict[str, Counter[str]]:
    """What the broker holds after a crash-free run of the same inputs."""
    journal = InMemoryLaneWorkJournal()
    orchestrator = make_orchestrator(journal)
    for ingress in inputs:
        orchestrator.handle(ingress)
    return _census(journal)


def _run(inputs: list[ModelLaneWorkIngress], fail_at: int | None) -> CrashingJournal:
    journal = CrashingJournal(fail_at)
    orchestrator = make_orchestrator(journal)
    for ingress in inputs:
        try:
            orchestrator.handle(ingress)
        except CrashError:
            orchestrator = make_orchestrator(journal)
            orchestrator.recover()
            orchestrator.handle(ingress)  # the uncommitted input is redelivered
    return journal


def _stable(census: dict[str, Counter[str]]) -> dict[str, list[str]]:
    """Output ids per role, ignoring the reply ids' correlation suffix (a new run has new ones)."""
    return {
        role: sorted(ids) for role, ids in census.items() if role != "step_completed"
    }


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_run_reaches_its_terminal_on_the_in_memory_bus(scenario: str) -> None:
    build, kind = SCENARIOS[scenario]
    rig = build()
    journal = rig.orchestrator.journal
    terminals = journal.read("terminal")
    assert [payload["kind"] for _, payload in terminals] == [kind]
    assert rig.name == kind


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_crash_at_every_publish_position_loses_nothing_and_duplicates_nothing(
    scenario: str,
) -> None:
    build, _ = SCENARIOS[scenario]
    inputs = build().sent
    clean = _run(inputs, None)
    total = clean.count
    assert total > len(
        inputs
    )  # positive control: a run publishes more than one message per input
    baseline = _stable(_census(clean))
    crashed_positions = 0
    for position in range(1, total + 1):
        journal = _run(inputs, position)
        assert journal.crashed, f"position {position} was never reached"
        crashed_positions += 1
        census = _census(journal)
        for role, counts in census.items():
            repeated = {oid: n for oid, n in counts.items() if n > 1}
            assert repeated == {}, f"position {position}: {role} duplicated {repeated}"
        assert _stable(census) == baseline, (
            f"position {position} changed what was published"
        )
        receipts = census["transition"]
        assert len(receipts) == len(inputs)
        assert sum(census["terminal"].values()) == 1
        commands = sum(
            sum(census[role].values())
            for role in ("ledger_query", "lab_proof", "repo_write", "handoff")
        )
        assert commands == sum(
            sum(baseline_ids.count(oid) for oid in set(baseline_ids))
            for role, baseline_ids in baseline.items()
            if role in ("ledger_query", "lab_proof", "repo_write", "handoff")
        )
    assert crashed_positions == total


def test_a_crash_before_the_offset_commit_replays_the_input_and_publishes_nothing_new() -> (
    None
):
    inputs = _handed_off().sent
    journal = CrashingJournal(None)
    orchestrator = make_orchestrator(journal)
    for ingress in inputs:
        orchestrator.handle(ingress)
    before = _census(journal)
    restarted = make_orchestrator(journal)
    restarted.recover()
    for ingress in inputs:  # every offset uncommitted: the whole log replays
        restarted.handle(ingress)
    assert _census(journal) == before


def test_a_command_already_on_the_broker_is_not_sent_again_on_recovery() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    rig.send("worker_started", lane_name="lane-x")
    journal = rig.orchestrator.journal
    queries = journal.read("ledger_query")
    assert len(queries) == 1
    restarted = LaneWorkOrchestrator(journal=journal, clock=lambda: T0)
    restarted.recover()
    assert journal.read("ledger_query") == queries


def test_the_roles_are_the_contracts_topics() -> None:
    topics = {topic_for(role) for role in ROLES}
    assert len(topics) == len(ROLES)
    assert all(topic.startswith("onex.") for topic in topics)
    assert isinstance(ModelLaneWorkEvent, type)
