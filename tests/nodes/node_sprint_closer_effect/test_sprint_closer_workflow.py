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
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.models.model_ticket_io import ModelTicketProjectDates
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_prs import (
    PrEvidence,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    LinearIssue,
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
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumChangeKind,
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
    """The closer's Linear client over a fake: what the write node would answer, and what was written."""

    def __init__(self, tickets: dict[str, str]) -> None:
        self.tickets = tickets  # id -> assignee
        self.comments: list[tuple[str, str]] = []
        self.states: list[str] = []

    def open_issue_ids(self, project_id: str) -> list[str]:
        return sorted(self.tickets)

    def sprint_projects(self) -> list[ModelTicketProjectDates]:
        return [
            ModelTicketProjectDates(id="p", start_date=date.min, target_date=date.max)
        ]

    def read_issue(
        self, ticket_id: str, *, include_attachments: bool = False
    ) -> LinearIssue:
        return LinearIssue(
            identifier=ticket_id,
            title=ticket_id,
            description=DESC,
            state="In Review",
            assignee=self.tickets[ticket_id],
        )

    def comment(self, ticket_id: str, body: str) -> None:
        self.comments.append((ticket_id, body))

    def set_done(self, ticket_id: str) -> None:
        self.states.append(ticket_id)


class FakePrs:
    def read(self, links: tuple[tuple[str, int], ...]) -> PrEvidence:
        return PrEvidence(
            merged=("omninode-ai/example#1",), change_kind=EnumChangeKind.NON_RUNTIME
        )


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
        "dry_run": True,
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
    sources = LiveSprintCloserSources(
        linear=cast(Any, linear),
        dod=cast(Any, dod),
        prs=cast(Any, FakePrs()),
        ledger_read_argv=(),
    )
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
    assert linear.states == []
    assert linear.comments == []
    assert all(
        o.dry_run and not o.commented and o.state_set is None for o in result.outcomes
    )


async def test_repeatedly_failing_ticket_resumes_without_rerunning_finished(
    env: WorkflowEnvironment,
) -> None:
    linear = FakeLinear({"OMN-1": "Owner", "OMN-2": "Owner", "OMN-3": "Owner"})
    dod = FakeDod({"OMN-1": PASS, "OMN-2": PASS, "OMN-3": PASS})
    dod.gated = {"OMN-2"}
    sources = LiveSprintCloserSources(
        linear=cast(Any, linear),
        dod=cast(Any, dod),
        prs=cast(Any, FakePrs()),
        ledger_read_argv=(),
    )
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
        linear=cast(Any, linear),
        dod=cast(Any, FakeDod({"OMN-1": FAIL})),
        ledger_read_argv=(),
    )
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


@pytest.mark.asyncio
async def test_over_budget_continues_with_unfinished_tickets_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from temporalio import workflow

    from omnimarket.nodes.node_sprint_closer_effect.handlers.temporal_sprint_closer import (
        LIST_ACTIVITY,
    )
    from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
        ModelTicketOutcome,
    )

    class ContinuedError(Exception):
        pass

    candidates = [f"OMN-{i}" for i in range(1, 27)]
    attempted: list[str] = []
    lists = 0

    async def execute(name: str, *, args: list[str], **kwargs: object) -> object:
        nonlocal lists
        if name == LIST_ACTIVITY:
            lists += 1
            return candidates
        ticket_id = args[1]
        attempted.append(ticket_id)
        return ModelTicketOutcome(
            ticket_id=ticket_id,
            verdict="leave",
            action=EnumCloserAction.NO_CHANGE,
        ).model_dump_json()

    def continue_as_new(plan_json: str) -> None:
        raise ContinuedError(plan_json)

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "continue_as_new", continue_as_new)
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(workflow_id="sprint"))
    monkeypatch.setattr(workflow, "patched", lambda _: True)

    with pytest.raises(ContinuedError) as first:
        await SprintCloserWorkflow().run(_plan(dry_run=True))
    assert attempted == candidates[:25]
    result = ModelSprintCloserRunResult.model_validate_json(
        await SprintCloserWorkflow().run(first.value.args[0])
    )
    assert attempted == candidates
    assert lists == 1
    assert [outcome.ticket_id for outcome in result.outcomes] == candidates


async def test_temporal_continuation_preserves_outcomes_after_a_ticket_exhausts_retries(
    env: WorkflowEnvironment,
) -> None:
    tickets = {"OMN-1": "Owner", "OMN-2": "Owner", "OMN-3": "Collaborator"}
    linear = FakeLinear(tickets)
    dod = FakeDod(dict.fromkeys(tickets, PASS))
    dod.gated = {"OMN-2"}
    sources = LiveSprintCloserSources(
        linear=cast(Any, linear), dod=cast(Any, dod), prs=cast(Any, FakePrs())
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with _worker(env.client, sources, pool):
            handle = await env.client.start_workflow(
                WORKFLOW_NAME,
                _plan(tickets_per_run=2, activity_max_attempts=2),
                id=f"batch-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            result = ModelSprintCloserRunResult.model_validate_json(
                await handle.result()
            )
    assert dod.calls == Counter({"OMN-1": 1, "OMN-2": 2, "OMN-3": 1})
    assert [o.ticket_id for o in result.outcomes] == list(tickets)
    assert (
        result.outcomes[1]
        .missing[0]
        .startswith("ticket activity exhausted its retries")
    )
    assert result.outcomes[2].action is EnumCloserAction.COMMENT_EVIDENCE
    assert linear.states == linear.comments == []


async def test_schedule_serializes_a_dry_run_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from omnimarket.nodes.node_sprint_closer_effect import runtime_sprint_closer

    client = AsyncMock()
    monkeypatch.setattr(
        runtime_sprint_closer, "_client", AsyncMock(return_value=client)
    )
    plan = ModelSprintCloserPlan.model_validate_json(_plan(dry_run=False))
    assert (
        await runtime_sprint_closer.create_schedule(
            "unused", "default", QUEUE, plan, "sprint-closer-every-2h", 2
        )
        == "created"
    )
    schedule = client.create_schedule.await_args.args[1]
    assert schedule.spec.intervals[0].every.total_seconds() == 7200
    assert ModelSprintCloserPlan.model_validate_json(schedule.action.args[0]).dry_run
