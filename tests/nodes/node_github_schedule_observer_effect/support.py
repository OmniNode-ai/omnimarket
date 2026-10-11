# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded transport, fake clone reader and builders for the GitHub schedule observer tests (OMN-20803).

The fixtures under tests/fixtures/github_schedule_observer are GitHub REST
responses laid out as the documented API returns them (headers included, with
the paginated Link headers); no test reads GitHub.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.handler_github_schedule_observer import (
    HandlerGithubScheduleObserver,
    SeamEvent,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models import (
    ModelClonedRepository,
    ModelClonedWorkflow,
    ModelGithubScheduleObserverRequest,
    ModelObservedRepository,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols import (
    ScheduleCloneError,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "github_schedule_observer"
OVERLAY = str(FIXTURES / "overlay.yaml")
REPOSITORY = "example-owner/example-repo"
HEAD = "d" * 40
#: Before the fixtures' x-ratelimit-reset (1791700000).
CLOCK_EPOCH = 1791600000.0


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class ModelRecordedExchange(BaseModel):
    """One recorded GitHub exchange: the request path and the response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: int
    headers: dict[str, str]
    body: dict[str, object] | None = None


_EXCHANGES = TypeAdapter(list[ModelRecordedExchange])


def load_exchanges(*names: str) -> list[ModelRecordedExchange]:
    exchanges: list[ModelRecordedExchange] = []
    for name in names:
        exchanges.extend(
            _EXCHANGES.validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))
        )
    return exchanges


class RecordedTransport:
    """Replay recorded exchanges by request path; an unrecorded request fails the test."""

    def __init__(self, *names: str) -> None:
        self._queues: dict[str, deque[ModelGithubHttpResponse]] = defaultdict(deque)
        for exchange in load_exchanges(*names):
            self._queues[exchange.path].append(
                ModelGithubHttpResponse(
                    status=exchange.status,
                    headers=exchange.headers,
                    body=exchange.body,
                )
            )
        self.requests: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.requests.append(request)
        queue = self._queues.get(request.path)
        assert queue, f"unrecorded request {request.method} {request.path}"
        return queue.popleft()

    @property
    def paths(self) -> list[str]:
        return [r.path for r in self.requests]


class FakeCloneReader:
    def __init__(self, clone: ModelClonedRepository | ScheduleCloneError) -> None:
        self._clone = clone
        self.read: list[str] = []

    def read_clone(self, clone_dir: Path) -> ModelClonedRepository:
        self.read.append(clone_dir.name)
        if isinstance(self._clone, ScheduleCloneError):
            raise self._clone
        return self._clone


def current_clone(
    *workflows: str, head: str = HEAD, remote: str = HEAD
) -> ModelClonedRepository:
    return ModelClonedRepository(
        head_sha=head,
        remote_default_head_sha=remote,
        default_branch="main",
        workflows=tuple(
            ModelClonedWorkflow(path=f".github/workflows/{w}", crons=("0 * * * *",))
            for w in workflows
        ),
    )


def request_at(
    observed_at: str,
    tmp_path: Path,
    *,
    runtime_paths: tuple[str, ...] = (),
    **extra: object,
) -> ModelGithubScheduleObserverRequest:
    return ModelGithubScheduleObserverRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "observed_at": at(observed_at),
            "overlay_path": OVERLAY,
            "state_path": str(tmp_path / "state.json"),
            "clone_root": str(tmp_path / "clones"),
            "repositories": [
                ModelObservedRepository(
                    repository=REPOSITORY, runtime_paths=runtime_paths
                )
            ],
            **extra,
        }
    )


def handler_for(
    transport: RecordedTransport, clone: ModelClonedRepository | ScheduleCloneError
) -> HandlerGithubScheduleObserver:
    return HandlerGithubScheduleObserver(
        transport,
        clone_reader=FakeCloneReader(clone),
        clock=lambda: CLOCK_EPOCH,
    )


def events_of[E: BaseModel](output: tuple[SeamEvent, ...], kind: type[E]) -> list[E]:
    return [e for e in output if isinstance(e, kind)]


def run_events(output: tuple[SeamEvent, ...]) -> list[ModelAutomationRunObserved]:
    return events_of(output, ModelAutomationRunObserved)


def verdict_events(
    output: tuple[SeamEvent, ...],
) -> list[ModelAutomationLivenessVerdictEvent]:
    return events_of(output, ModelAutomationLivenessVerdictEvent)
