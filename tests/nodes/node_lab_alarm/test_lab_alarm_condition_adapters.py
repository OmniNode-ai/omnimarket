# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-backed readers of the canary and projection conditions (OMN-24400): the request each builds and how it reads the answer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from omnimarket.handlers.scm.handler_gateway_read_client import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.nodes.node_lab_alarm.adapters.bus_adapters import (
    BusProjectionNewest,
    GatewayCanaryRunSource,
    work_ledger_query_topics,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelConditionPersistence,
    ModelDelegationChainCanarySettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    CanaryRunsUnreadableError,
    ProjectionUnreadableError,
)

_SETTINGS = ModelDelegationChainCanarySettings(
    repository="example-org/example-repo",
    workflow="example-canary.yml",
    branch="main",
    events=("schedule", "workflow_dispatch"),
    max_age_seconds=10800,
    per_page=7,
    timeout_seconds=45,
    persistence=ModelConditionPersistence(persist_runs=3, streak_path="streaks.json"),
)


def _run(run_id: int, event: str, conclusion: str | None) -> ModelWorkflowRunSummary:
    return ModelWorkflowRunSummary(
        id=run_id,
        name="canary",
        workflow_id=1,
        head_branch="main",
        head_sha="a" * 40,
        event=event,
        status="completed",
        conclusion=conclusion,
        run_number=run_id,
        run_attempt=1,
        created_at="2026-11-12T04:00:00Z",
        updated_at="2026-11-12T04:05:00Z",
        html_url=f"https://example.invalid/runs/{run_id}",
    )


class _Client:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[ModelGithubGatewayRequest] = []

    def read(self, request: ModelGithubGatewayRequest) -> Any:
        self.requests.append(request)
        answer = self.answers[request.event or ""]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _runs_result(*runs: ModelWorkflowRunSummary) -> ModelWorkflowRunsResult:
    return ModelWorkflowRunsResult(
        repo="example-org/example-repo",
        total_count=len(runs),
        truncated=False,
        runs=list(runs),
    )


def test_gateway_canary_source_asks_for_completed_runs_of_each_admitted_event() -> None:
    client = _Client(
        {
            "schedule": _runs_result(_run(1, "schedule", "success")),
            "workflow_dispatch": _runs_result(_run(2, "workflow_dispatch", "failure")),
        }
    )
    runs = GatewayCanaryRunSource(_SETTINGS, client=client).read()  # type: ignore[arg-type]
    assert [r.id for r in runs] == [1, 2]
    assert [r.conclusion for r in runs] == ["success", "failure"]
    assert runs[0].created_at == datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
    assert [(q.operation, q.event, q.status, q.limit) for q in client.requests] == [
        (EnumGithubGatewayOperation.WORKFLOW_RUNS, "schedule", "completed", 7),
        (EnumGithubGatewayOperation.WORKFLOW_RUNS, "workflow_dispatch", "completed", 7),
    ]
    assert {(q.repo, q.workflow, q.branch) for q in client.requests} == {
        ("example-org/example-repo", "example-canary.yml", "main")
    }


def test_gateway_canary_source_a_gateway_that_does_not_answer_is_unreadable() -> None:
    client = _Client(
        {"schedule": GatewayReadError("no answer on lane 'dev' within 45s")}
    )
    with pytest.raises(CanaryRunsUnreadableError, match="no answer"):
        GatewayCanaryRunSource(_SETTINGS, client=client).read()  # type: ignore[arg-type]


def test_gateway_canary_source_an_untyped_answer_is_unreadable() -> None:
    client = _Client({"schedule": object(), "workflow_dispatch": object()})
    with pytest.raises(CanaryRunsUnreadableError, match="not workflow runs"):
        GatewayCanaryRunSource(_SETTINGS, client=client).read()  # type: ignore[arg-type]


def _terminal(payload: dict[str, Any], *, failed: bool = False) -> ModelScmTerminal:
    topics = work_ledger_query_topics()
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid.uuid4(),
            command_topic=topics.command,
            command_envelope_id=uuid.uuid4(),
            terminal_topic=topics.success,
            terminal_envelope_id=None,
            terminal_partition=0,
            terminal_offset="1",
        ),
        failed=failed,
        payload=payload,
    )


def _result_payload(newest: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "correlation_id": str(uuid.uuid4()),
        "query": "rows",
        "window_since": None,
        "window_until": "2026-11-12T04:00:00Z",
        "freshness": None
        if newest is None
        else {"newest_row_ts": newest, "newest_projected_at": newest, "rows_read": 1},
        **extra,
    }


class _Call:
    def __init__(self, terminal: ModelScmTerminal) -> None:
        self.terminal = terminal
        self.requests: list[BaseModel] = []

    def __call__(
        self, request: BaseModel, correlation_id: uuid.UUID
    ) -> ModelScmTerminal:
        self.requests.append(request)
        return self.terminal


def test_work_ledger_query_topics_come_from_the_nodes_contract() -> None:
    topics = work_ledger_query_topics()
    assert topics.command.endswith("work-ledger-query-requested.v1")
    assert topics.success.endswith("work-ledger-query-completed.v1")


def test_bus_projection_newest_asks_one_paged_row_since_the_window_start() -> None:
    since = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)
    call = _Call(_terminal(_result_payload("2026-11-12T03:59:00Z")))
    newest = BusProjectionNewest("example-lane", 30, call=call).newest(since)
    assert newest == datetime(2026, 11, 12, 3, 59, tzinfo=UTC)
    [request] = call.requests
    assert request.model_dump(mode="json")["query"] == "rows"
    assert request.model_dump(mode="json")["since"] == "2026-11-11T04:00:00Z"
    assert request.model_dump(mode="json")["page_size"] == 1


def test_bus_projection_newest_reads_an_answer_wrapped_under_result() -> None:
    call = _Call(_terminal({"result": _result_payload("2026-11-12T03:59:00Z")}))
    newest = BusProjectionNewest("example-lane", 30, call=call).newest(
        datetime(2026, 11, 11, tzinfo=UTC)
    )
    assert newest == datetime(2026, 11, 12, 3, 59, tzinfo=UTC)


def test_bus_projection_newest_an_empty_window_is_none_not_an_error() -> None:
    call = _Call(_terminal(_result_payload(None)))
    assert (
        BusProjectionNewest("example-lane", 30, call=call).newest(
            datetime(2026, 11, 11, tzinfo=UTC)
        )
        is None
    )


@pytest.mark.parametrize(
    "terminal",
    [
        _terminal({"status": "failed", "error": "database down"}),
        _terminal({"result": "not an object"}),
        _terminal(_result_payload("2026-11-12T03:59:00Z", error="database error")),
        _terminal({"query": "rows"}),
    ],
    ids=["failed-terminal", "no-result", "error-set", "malformed"],
)
def test_bus_projection_newest_an_answer_that_is_not_a_result_is_unreadable(
    terminal: ModelScmTerminal,
) -> None:
    with pytest.raises(ProjectionUnreadableError):
        BusProjectionNewest("example-lane", 30, call=_Call(terminal)).newest(
            datetime(2026, 11, 11, tzinfo=UTC)
        )
