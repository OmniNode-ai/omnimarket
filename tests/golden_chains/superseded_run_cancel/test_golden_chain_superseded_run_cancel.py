# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_superseded_run_cancel_effect (OMN-17427).

The shapes the 2026-10-14 hand sweeps met, run through the handler against a
recorded GitHub fake. The negative controls are the point: a run on a PR's
current head is never cancelled, whatever a watcher snapshot says, and a
merge_group run is never judged against a queue read that is not complete.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_superseded_run_cancel_effect.handlers.handler_superseded_run_cancel import (
    HandlerSupersededRunCancel,
)
from omnimarket.nodes.node_superseded_run_cancel_effect.models import (
    EnumSupersededReason,
    ModelSupersededRunSweepRequest,
    ModelSupersededRunSweepResult,
)

_REPO = "OmniNode-ai/omnimarket"
_CORR = UUID("00000000-0000-4000-a000-0000000000a1")
_OLD = "a" * 40
_NEW = "b" * 40
_NEWER = "c" * 40
_GROUP_OLD = "d" * 40
_GROUP_NEW = "e" * 40
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_superseded_run_cancel_effect/contract.yaml"
)


def _run(
    run_id: int,
    *,
    event: str = "pull_request",
    status: str = "in_progress",
    sha: str = _OLD,
    created: str = "2026-10-14T23:30:00Z",
    prs: tuple[int, ...] = (650,),
    branch: str = "feature/x",
    name: str = "CI",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "status": status,
        "head_sha": sha,
        "head_branch": branch,
        "created_at": created,
        "pull_requests": [{"number": n} for n in prs],
    }


def _group_run(
    run_id: int, sha: str, *, base: str = "main", pr: int = 650
) -> dict[str, object]:
    return _run(
        run_id,
        event="merge_group",
        status="queued",
        sha=sha,
        prs=(),
        branch=f"gh-readonly-queue/{base}/pr-{pr}-{'f' * 40}",
    )


def _queue_body(shas: list[str], *, total: int | None = None) -> dict[str, object]:
    nodes = [{"headCommit": {"oid": s}, "pullRequest": {"number": 1}} for s in shas]
    return {
        "data": {
            "repository": {
                "mergeQueue": {
                    "entries": {
                        "totalCount": len(nodes) if total is None else total,
                        "nodes": nodes,
                    }
                }
            }
        }
    }


class _GithubFake:
    """Serves the run list by status, the queue read by base, and records cancels."""

    def __init__(
        self,
        *,
        queued: list[dict[str, object]] | None = None,
        in_progress: list[dict[str, object]] | None = None,
        queues: dict[str, object] | None = None,
        list_status: int = 200,
        cancel_status: dict[int, int] | None = None,
    ) -> None:
        self.by_status = {"queued": queued or [], "in_progress": in_progress or []}
        self.queues = queues or {}
        self.list_status = list_status
        self.cancel_status = cancel_status or {}
        self.cancels: list[int] = []
        self.sent: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        if request.path == GITHUB_GRAPHQL_PATH:
            assert request.body is not None
            variables = request.body["variables"]
            assert isinstance(variables, dict)
            served = self.queues.get(str(variables["branch"]))
            if isinstance(served, int):
                return ModelGithubHttpResponse(status=served, headers={}, body=None)
            if served is None:
                return ModelGithubHttpResponse(
                    status=200,
                    headers={},
                    body={"data": {"repository": {"mergeQueue": None}}},
                )
            assert isinstance(served, dict)
            return ModelGithubHttpResponse(status=200, headers={}, body=served)
        if request.method == "POST":
            run_id = int(request.path.split("/")[-2])
            assert request.path.endswith("/cancel")
            self.cancels.append(run_id)
            status = self.cancel_status.get(run_id, 202)
            return ModelGithubHttpResponse(
                status=status,
                headers={},
                body=None
                if status == 202
                else {"message": "Cannot cancel a workflow run that is completed."},
            )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.path).query)
        if self.list_status != 200:
            return ModelGithubHttpResponse(
                status=self.list_status, headers={}, body={"message": "boom"}
            )
        runs = self.by_status[query["status"][0]]
        return ModelGithubHttpResponse(
            status=200, headers={}, body={"workflow_runs": runs}
        )


async def _sweep(
    fake: _GithubFake, *, dry_run: bool = False
) -> tuple[list[int], ModelSupersededRunSweepResult]:
    handler = HandlerSupersededRunCancel(transport=fake)
    result = await handler.handle(
        ModelSupersededRunSweepRequest(
            correlation_id=_CORR, repo=_REPO, dry_run=dry_run
        )
    )
    return sorted(fake.cancels), result


# --- pull_request runs ---------------------------------------------------------


@pytest.mark.asyncio
async def test_old_head_runs_are_cancelled_every_workflow_of_them() -> None:
    fake = _GithubFake(
        queued=[
            _run(1, status="queued", name="CI"),
            _run(2, status="queued", name="Lint"),
        ],
        in_progress=[
            _run(3, sha=_NEW, created="2026-10-14T23:35:34Z", name="CI"),
            _run(4, sha=_NEW, created="2026-10-14T23:35:34Z", name="Lint"),
        ],
    )
    cancels, result = await _sweep(fake)
    assert cancels == [1, 2]
    assert [e.run_id for e in result.cancelled] == [1, 2]
    for event in result.cancelled:
        assert event.repo == _REPO
        assert event.reason is EnumSupersededReason.PR_HEAD_MOVED
        assert event.pr_number == 650
        assert event.current_head_sha == _NEW


@pytest.mark.asyncio
async def test_negative_control_run_on_the_current_head_is_never_cancelled() -> None:
    """The 2026-10-14 mistaken cancel: a push newer than the watcher snapshot.

    The snapshot (tick 23:35:21Z) knew head _OLD; runs on _NEW were created at
    23:35:34Z. The run list itself holds both heads, so the rule keeps _NEW.
    """
    fake = _GithubFake(
        in_progress=[
            _run(10, sha=_OLD, created="2026-10-14T23:20:00Z"),
            _run(11, sha=_NEW, created="2026-10-14T23:35:34Z", name="CI"),
            _run(12, sha=_NEW, created="2026-10-14T23:35:34Z", name="Lint"),
            _run(13, sha=_NEW, created="2026-10-14T23:35:35Z", name="Typecheck"),
        ],
    )
    cancels, result = await _sweep(fake)
    assert cancels == [10]
    assert {e.run_id for e in result.cancelled}.isdisjoint({11, 12, 13})


@pytest.mark.asyncio
async def test_a_pr_with_one_head_loses_nothing() -> None:
    fake = _GithubFake(
        queued=[_run(20, status="queued"), _run(21, status="queued", name="Lint")],
        in_progress=[_run(22, name="Typecheck")],
    )
    cancels, result = await _sweep(fake)
    assert cancels == []
    assert result.cancelled == ()
    assert result.runs_read == 3


@pytest.mark.asyncio
async def test_three_pushes_keep_only_the_newest_head() -> None:
    fake = _GithubFake(
        queued=[_run(30, sha=_OLD, created="2026-10-14T22:00:00Z", status="queued")],
        in_progress=[
            _run(31, sha=_NEW, created="2026-10-14T22:30:00Z"),
            _run(32, sha=_NEWER, created="2026-10-14T23:00:00Z"),
        ],
    )
    cancels, _ = await _sweep(fake)
    assert cancels == [30, 31]


@pytest.mark.asyncio
async def test_a_rerun_of_an_old_head_run_is_still_superseded() -> None:
    """A rerun keeps created_at, so the old run keeps its place behind the push."""
    fake = _GithubFake(
        in_progress=[
            _run(40, sha=_OLD, created="2026-10-14T20:00:00Z"),
            _run(41, sha=_NEW, created="2026-10-14T21:00:00Z"),
        ],
    )
    cancels, _ = await _sweep(fake)
    assert cancels == [40]


@pytest.mark.asyncio
async def test_two_prs_are_judged_apart() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(50, sha=_OLD, prs=(650,), created="2026-10-14T20:00:00Z"),
            _run(51, sha=_NEW, prs=(650,), created="2026-10-14T21:00:00Z"),
            _run(52, sha=_OLD, prs=(651,), created="2026-10-14T20:00:00Z"),
        ],
    )
    cancels, _ = await _sweep(fake)
    assert cancels == [50]


@pytest.mark.asyncio
async def test_runs_of_other_events_or_without_a_pr_are_never_selected() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(60, event="push", sha=_OLD, prs=(), created="2026-10-14T20:00:00Z"),
            _run(
                61, event="schedule", sha=_NEW, prs=(), created="2026-10-14T21:00:00Z"
            ),
            _run(
                62,
                event="pull_request",
                sha=_OLD,
                prs=(),
                created="2026-10-14T20:00:00Z",
            ),
            _run(
                63,
                event="pull_request",
                sha=_NEW,
                prs=(),
                created="2026-10-14T21:00:00Z",
            ),
        ],
    )
    cancels, _ = await _sweep(fake)
    assert cancels == []


@pytest.mark.asyncio
async def test_the_runs_of_both_statuses_are_read_and_a_duplicate_counts_once() -> None:
    moved = _run(70, sha=_NEW, created="2026-10-14T21:00:00Z")
    fake = _GithubFake(
        queued=[
            _run(71, sha=_OLD, created="2026-10-14T20:00:00Z", status="queued"),
            moved,
        ],
        in_progress=[moved],
    )
    cancels, result = await _sweep(fake)
    assert cancels == [71]
    assert result.runs_read == 2


# --- merge_group runs ----------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_group_run_out_of_the_queue_is_cancelled() -> None:
    """A rebuilt entry: the old group commit left the queue, the new one is in it."""
    fake = _GithubFake(
        queued=[_group_run(80, _GROUP_OLD), _group_run(81, _GROUP_NEW)],
        queues={"main": _queue_body([_GROUP_NEW])},
    )
    cancels, result = await _sweep(fake)
    assert cancels == [80]
    (event,) = result.cancelled
    assert event.reason is EnumSupersededReason.MERGE_GROUP_LEFT_QUEUE
    assert event.base_branch == "main"
    assert event.repo == _REPO
    assert event.run_id == 80


@pytest.mark.asyncio
async def test_negative_control_merge_group_run_still_in_the_queue_is_kept() -> None:
    fake = _GithubFake(
        queued=[_group_run(90, _GROUP_NEW)],
        queues={"main": _queue_body([_GROUP_NEW])},
    )
    cancels, _ = await _sweep(fake)
    assert cancels == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "served",
    [
        pytest.param(502, id="read-failed"),
        pytest.param(None, id="no-queue-on-the-branch"),
        pytest.param(_queue_body([_GROUP_NEW], total=101), id="entries-cut-short"),
        pytest.param({"errors": [{"message": "x"}]}, id="graphql-error"),
    ],
)
async def test_negative_control_an_incomplete_queue_read_judges_nothing(
    served: object,
) -> None:
    fake = _GithubFake(queued=[_group_run(100, _GROUP_OLD)], queues={"main": served})
    cancels, result = await _sweep(fake)
    assert cancels == []
    assert result.unjudged_merge_group_runs == 1


@pytest.mark.asyncio
async def test_the_queue_is_read_after_the_run_list_and_once_per_base() -> None:
    fake = _GithubFake(
        queued=[
            _group_run(110, _GROUP_OLD, base="main"),
            _group_run(111, _GROUP_NEW, base="main"),
            _group_run(112, _GROUP_OLD, base="release/1.0"),
        ],
        queues={"main": _queue_body([_GROUP_NEW]), "release/1.0": _queue_body([])},
    )
    cancels, _ = await _sweep(fake)
    assert cancels == [110, 112]
    kinds = ["queue" if r.path == GITHUB_GRAPHQL_PATH else r.method for r in fake.sent]
    first_queue = kinds.index("queue")
    assert "GET" in kinds[:first_queue]
    assert kinds.count("queue") == 2
    assert kinds.index("POST") > max(i for i, k in enumerate(kinds) if k == "queue")


# --- failures and modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_run_list_cancels_nothing() -> None:
    fake = _GithubFake(in_progress=[_run(120)], list_status=502)
    cancels, result = await _sweep(fake)
    assert cancels == []
    assert result.runs_read == 0
    assert result.read_error is not None


@pytest.mark.asyncio
async def test_a_cancel_github_refuses_is_reported_and_emits_no_event() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(130, sha=_OLD, created="2026-10-14T20:00:00Z"),
            _run(131, sha=_OLD, created="2026-10-14T20:00:00Z", name="Lint"),
            _run(132, sha=_NEW, created="2026-10-14T21:00:00Z"),
        ],
        cancel_status={130: 409},
    )
    cancels, result = await _sweep(fake)
    assert cancels == [130, 131]
    assert [e.run_id for e in result.cancelled] == [131]
    assert [(s.run_id, s.http_status) for s in result.not_cancelled] == [(130, 409)]


@pytest.mark.asyncio
async def test_dry_run_reports_the_cancels_and_sends_none() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(140, sha=_OLD, created="2026-10-14T20:00:00Z"),
            _run(141, sha=_NEW, created="2026-10-14T21:00:00Z"),
        ],
    )
    cancels, result = await _sweep(fake, dry_run=True)
    assert cancels == []
    assert [e.run_id for e in result.cancelled] == [140]
    assert result.cancelled[0].dry_run is True


@pytest.mark.asyncio
async def test_the_run_list_order_does_not_change_the_decision() -> None:
    runs = [
        _run(150, sha=_OLD, created="2026-10-14T20:00:00Z"),
        _run(151, sha=_NEW, created="2026-10-14T21:00:00Z"),
        _run(152, sha=_NEW, created="2026-10-14T21:00:00Z", name="Lint"),
    ]
    forward, _ = await _sweep(_GithubFake(in_progress=list(runs)))
    backward, _ = await _sweep(_GithubFake(in_progress=list(reversed(runs))))
    assert forward == backward == [150]


# --- the contract --------------------------------------------------------------


def test_the_contract_declares_the_cancel_event_and_sweep_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert "onex.evt.omnimarket.superseded-run-cancelled.v1" in bus["publish_topics"]
    assert (
        "onex.evt.omnimarket.superseded-run-sweep-completed.v1" in bus["publish_topics"]
    )
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.superseded-run-sweep-requested.v1"
    ]


# --- events on the bus ---------------------------------------------------------


class _BusFake:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self._fail = fail

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        if self._fail:
            raise ConnectionError("bus down")
        self.published.append((topic, key, value))


@pytest.mark.asyncio
async def test_one_cancelled_event_per_cancel_then_the_completed_event() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(160, sha=_OLD, created="2026-10-14T20:00:00Z"),
            _run(161, sha=_OLD, created="2026-10-14T20:00:00Z", name="Lint"),
            _run(162, sha=_NEW, created="2026-10-14T21:00:00Z"),
        ],
    )
    bus = _BusFake()
    handler = HandlerSupersededRunCancel(transport=fake, event_bus=bus)
    await handler.handle(
        ModelSupersededRunSweepRequest(correlation_id=_CORR, repo=_REPO)
    )
    topics = [t for t, _, _ in bus.published]
    assert topics == [
        "onex.evt.omnimarket.superseded-run-cancelled.v1",
        "onex.evt.omnimarket.superseded-run-cancelled.v1",
        "onex.evt.omnimarket.superseded-run-sweep-completed.v1",
    ]
    first = json.loads(bus.published[0][2])
    assert (first["repo"], first["run_id"], first["reason"]) == (
        _REPO,
        160,
        "pr_head_moved",
    )


@pytest.mark.asyncio
async def test_a_bus_failure_does_not_undo_or_raise_after_the_cancels() -> None:
    fake = _GithubFake(
        in_progress=[
            _run(170, sha=_OLD, created="2026-10-14T20:00:00Z"),
            _run(171, sha=_NEW, created="2026-10-14T21:00:00Z"),
        ],
    )
    handler = HandlerSupersededRunCancel(transport=fake, event_bus=_BusFake(fail=True))
    result = await handler.handle(
        ModelSupersededRunSweepRequest(correlation_id=_CORR, repo=_REPO)
    )
    assert fake.cancels == [170]
    assert [e.run_id for e in result.cancelled] == [170]
