# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sprint closer workflow on Temporal's time-skipping test server, with the production sources over fakes of
Linear and dod_verify: one activity per ticket, a failed ticket retried and resumed without re-running finished
tickets and a collaborator's ticket commented with its state left."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    LiveSprintCloserSources,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.temporal_sprint_closer import (
    SEEN_RUNNING_SIGNAL,
    TICKET_ACTIVITY,
    WORKFLOW_NAME,
    SprintCloserActivities,
    SprintCloserWorkflow,
)
from omnimarket.nodes.node_sprint_closer_effect.models import (
    EnumCheckStatus,
    EnumCloserAction,
    ModelCheckResult,
    ModelSprintCloserPlan,
    ModelSprintCloserRunResult,
)

pytestmark = pytest.mark.unit

QUEUE = "sprint-closer-test"
DESC = "## Acceptance criteria\n\n- [ ] AC1: one\n"
PASS = (
    ModelCheckResult(evidence_id="c1", status=EnumCheckStatus.PASS, binds=("AC1",)),
)
FAIL = (
    ModelCheckResult(
        evidence_id="c1", status=EnumCheckStatus.FAIL, binds=("AC1",), detail="exit 1"
    ),
)


class FakeLinear:
    def __init__(self, tickets: dict[str, str]) -> None:
        self.tickets = tickets  # id -> assignee
        self.comments: list[tuple[str, str]] = []
        self.states: list[tuple[str, str]] = []

    def open_issue_ids(self, project_id: str) -> list[str]:
        return sorted(self.tickets)

    def read_issue(self, ticket_id: str) -> dict[str, object]:
        return {
            "id": f"uuid-{ticket_id}",
            "identifier": ticket_id,
            "title": ticket_id,
            "description": DESC,
            "state": {"name": "In Review", "type": "started"},
            "assignee": {"name": self.tickets[ticket_id]},
            "team": {
                "states": {
                    "nodes": [{"id": "done-id", "name": "Done", "type": "completed"}]
                }
            },
            "comments": {"nodes": []},
        }

    def comment(self, issue_uuid: str, body: str) -> None:
        self.comments.append((issue_uuid, body))

    def set_state(self, issue_uuid: str, state_id: str) -> None:
        self.states.append((issue_uuid, state_id))


class FakeDod:
    """Counts runs per ticket; ``fail_until`` makes a ticket's run raise until the gate is opened."""

    def __init__(
        self,
        results: dict[str, tuple[ModelCheckResult, ...]],
        fail_first: set[str] | None = None,
    ) -> None:
        self.results = results
        self.fail_first = fail_first or set()
        self.calls: Counter[str] = Counter()
        self.gate = threading.Event()
        self.gated: set[str] = set()

    def run(
        self, ticket_id: str
    ) -> tuple[str | None, str | None, tuple[ModelCheckResult, ...]]:
        self.calls[ticket_id] += 1
        if ticket_id in self.fail_first and self.calls[ticket_id] == 1:
            raise RuntimeError(f"transient failure on {ticket_id}")
        if ticket_id in self.gated and not self.gate.is_set():
            raise RuntimeError(f"{ticket_id} waits for the gate")
        return (
            "OmniNode-ai/omnimarket@abc:contracts/x.yaml",
            None,
            self.results[ticket_id],
        )


def _plan(**kw: object) -> str:
    base: dict[str, object] = {
        "project_id": "p",
        "owner_assignees": ("Owner",),
        "actor": "test (none)",
        "dry_run": False,
        "trigger": "test",
        "retry_initial_seconds": 1,
        "activity_max_attempts": 5,
    }
    base.update(kw)
    return ModelSprintCloserPlan.model_validate(base).model_dump_json()


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _worker(
    client: Client, sources: LiveSprintCloserSources, pool: ThreadPoolExecutor
) -> Worker:
    acts = SprintCloserActivities(sources)
    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[SprintCloserWorkflow],
        activities=[acts.list_tickets, acts.run_ticket],
        activity_executor=pool,
    )


async def _ticket_activity_count(client: Client, wf_id: str) -> int:
    n = 0
    async for event in client.get_workflow_handle(wf_id).fetch_history_events():
        if (
            event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            and event.activity_task_scheduled_event_attributes.activity_type.name
            == TICKET_ACTIVITY
        ):
            n += 1
    return n


async def test_one_activity_per_ticket_retry_and_collaborator(
    env: WorkflowEnvironment,
) -> None:
    linear = FakeLinear(
        {"OMN-1": "Owner", "OMN-2": "Owner", "OMN-3": "Collaborator", "OMN-4": "Owner"}
    )
    dod = FakeDod(
        {"OMN-1": PASS, "OMN-2": PASS, "OMN-3": PASS, "OMN-4": FAIL},
        fail_first={"OMN-2"},
    )
    sources = LiveSprintCloserSources(linear=linear, dod=dod, ledger_path=None)  # type: ignore[arg-type]
    wf_id = f"closer-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with _worker(env.client, sources, pool):
            raw = await env.client.execute_workflow(
                WORKFLOW_NAME, _plan(), id=wf_id, task_queue=QUEUE
            )
    result = ModelSprintCloserRunResult.model_validate_json(raw)
    actions = {o.ticket_id: o.action for o in result.outcomes}
    assert actions == {
        "OMN-1": EnumCloserAction.SET_DONE,
        "OMN-2": EnumCloserAction.SET_DONE,
        "OMN-3": EnumCloserAction.COMMENT_EVIDENCE,
        "OMN-4": EnumCloserAction.COMMENT_MISSING,
    }
    # one scheduled activity per ticket; the failed attempt on OMN-2 was a retry of the same activity
    assert await _ticket_activity_count(env.client, wf_id) == 4
    assert dod.calls == Counter({"OMN-1": 1, "OMN-2": 2, "OMN-3": 1, "OMN-4": 1})
    # the collaborator's ticket is commented and keeps its state; owners' passing tickets are set Done
    assert sorted(s[0] for s in linear.states) == ["uuid-OMN-1", "uuid-OMN-2"]
    assert [c[0] for c in linear.comments].count("uuid-OMN-3") == 1
    assert all(body.startswith("actor: test (none)\n") for _, body in linear.comments)


async def test_repeatedly_failing_ticket_resumes_without_rerunning_finished(
    env: WorkflowEnvironment,
) -> None:
    linear = FakeLinear({"OMN-1": "Owner", "OMN-2": "Owner", "OMN-3": "Owner"})
    dod = FakeDod({"OMN-1": PASS, "OMN-2": PASS, "OMN-3": PASS})
    dod.gated = {"OMN-2"}
    sources = LiveSprintCloserSources(linear=linear, dod=dod, ledger_path=None)  # type: ignore[arg-type]
    wf_id = f"closer-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with _worker(env.client, sources, pool):
            handle = await env.client.start_workflow(
                WORKFLOW_NAME, _plan(), id=wf_id, task_queue=QUEUE
            )
            for _ in range(400):
                if dod.calls["OMN-2"] >= 2:
                    break
                await asyncio.sleep(0.05)
            assert dod.calls["OMN-2"] >= 2
            assert dod.calls["OMN-1"] == 1
            assert dod.calls["OMN-3"] == 0
            dod.gate.set()
            raw = await handle.result()
    result = ModelSprintCloserRunResult.model_validate_json(raw)
    assert [o.ticket_id for o in result.outcomes] == ["OMN-1", "OMN-2", "OMN-3"]
    assert dod.calls["OMN-1"] == 1
    assert dod.calls["OMN-3"] == 1
    assert await _ticket_activity_count(env.client, wf_id) == 3


async def test_seen_running_signal_start_records_the_mark(
    env: WorkflowEnvironment,
) -> None:
    linear = FakeLinear({"OMN-1": "Owner"})
    sources = LiveSprintCloserSources(
        linear=linear, dod=FakeDod({"OMN-1": FAIL}), ledger_path=None
    )  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with _worker(env.client, sources, pool):
            handle = await env.client.start_workflow(
                WORKFLOW_NAME,
                _plan(dry_run=True, trigger="seen_running:omnimarket#1"),
                id=f"seen-{uuid.uuid4()}",
                task_queue=QUEUE,
                start_signal=SEEN_RUNNING_SIGNAL,
                start_signal_args=["omnimarket#1"],
            )
            result = ModelSprintCloserRunResult.model_validate_json(
                await handle.result()
            )
    assert result.seen_running_marks == ("omnimarket#1",)
    assert linear.comments == []  # dry run writes nothing
    assert linear.states == []


def test_tests_dir_is_a_package() -> None:
    assert Path(__file__).with_name("__init__.py").is_file()
