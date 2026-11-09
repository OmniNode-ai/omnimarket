# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly Linear snapshot survives Linear's HTTP 429: it waits, reads only what changed, and keeps the last good file.

The recorded failure is the 2026-11-08T08:05Z fire of the host's ``linear-snapshot`` unit, which exited 1 with
``HTTPStatusError: Client error '429 Too Many Requests' for url 'https://api.linear.app/graphql'`` and
``issues=0``. ``test_recorded_429_*`` replays that answer through the production transport (``http_post``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot as handler_module,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    BACKOFF_BASE_S,
    HandlerLinearSnapshot,
    SnapshotRateLimitedError,
    retry_after_seconds,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshotRequest,
    ModelLinearSnapshotResult,
)
from omnimarket.nodes.node_linear_snapshot_effect.runtime_linear_snapshot import main

VIEWER = "7f1c0f6e-0000-4000-8000-000000000001"
OTHER = "7f1c0f6e-0000-4000-8000-0000000000aa"
T0 = datetime(2026, 11, 8, 7, 5, 0, tzinfo=UTC)


def node(
    n: int, updated: str = "2026-11-08T06:00:00.000Z", **over: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "identifier": f"OMN-{n}",
        "title": f"Ticket {n}",
        "description": f"Body {n}",
        "updatedAt": updated,
        "archivedAt": None,
        "priority": 2,
        "priorityLabel": "High",
        "state": {"name": "In Progress", "type": "started"},
        "assignee": {"id": VIEWER},
        "labels": {"nodes": [{"name": "machinery"}]},
        "project": {"name": "Delegation Pillar"},
    }
    return {**base, **over}


def page(nodes: list[dict[str, Any]], more: bool = False) -> dict[str, Any]:
    return {
        "viewer": {"id": VIEWER},
        "issues": {
            "pageInfo": {"hasNextPage": more, "endCursor": "c" if more else None},
            "nodes": nodes,
        },
    }


class Clock:
    """A settable clock and the waits the handler asked it to sleep."""

    def __init__(self, at: datetime = T0) -> None:
        self.at = at
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


class Script:
    """A transport answering in order; an exception in the script is raised."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, dict)
        return answer


def run(
    tmp_path: Path, post: Any, clock: Clock, **req: Any
) -> ModelLinearSnapshotResult:
    handler = HandlerLinearSnapshot(post, sleep=clock.sleep, now=clock.now)
    return handler.handle(ModelLinearSnapshotRequest(out_dir=str(tmp_path), **req))


def issues_in(tmp_path: Path, date: str = "2026-11-08") -> dict[str, dict[str, Any]]:
    doc = json.loads((tmp_path / f"linear-{date}.json").read_text(encoding="utf-8"))
    return {i["id"]: i for i in doc["issues"]}


def state_of(tmp_path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (tmp_path / "linear-snapshot-state.json").read_text(encoding="utf-8")
    )
    return loaded


def response(
    status: int, *, headers: dict[str, str] | None = None, body: Any = None
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        json=body,
        request=httpx.Request("POST", LINEAR_GRAPHQL_URL),
    )


def serve(
    monkeypatch: pytest.MonkeyPatch, *answers: httpx.Response
) -> list[httpx.Response]:
    """Put the answers behind ``httpx.post`` in order (the last repeats) and the key in the environment."""
    queue = list(answers)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    def post(url: str, **kwargs: Any) -> httpx.Response:
        assert url == LINEAR_GRAPHQL_URL
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(httpx, "post", post)
    return queue


# --- the recorded failure ------------------------------------------------------------------------


def test_recorded_429_waits_the_retry_after_and_writes_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the change this exact answer ended the run: failed, issues=0, exit 1, nothing written."""
    serve(
        monkeypatch,
        response(429, headers={"Retry-After": "7"}),
        response(200, body={"data": page([node(1), node(2)])}),
    )
    clock = Clock()
    result = run(tmp_path, handler_module.http_post, clock)
    assert (result.status, result.mode, result.issue_count) == ("written", "full", 2)
    assert clock.slept == [7.0]
    assert set(issues_in(tmp_path)) == {"OMN-1", "OMN-2"}


def test_recorded_429_without_a_snapshot_to_keep_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    serve(monkeypatch, response(429, headers={"Retry-After": "4000"}))
    assert main(["--out-dir", str(tmp_path), "--date", "2026-11-08"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("linear-snapshot | failed ")
    assert "429" in out
    assert not list(tmp_path.glob("linear-2*.json"))


def test_a_ratelimited_graphql_error_is_waited_out_like_a_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited = {
        "errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]
    }
    serve(
        monkeypatch,
        response(400, body=limited),
        response(200, body={"data": page([node(1)])}),
    )
    clock = Clock()
    result = run(tmp_path, handler_module.http_post, clock)
    assert result.status == "written"
    assert clock.slept == [BACKOFF_BASE_S]


# --- waiting -------------------------------------------------------------------------------------


def test_retry_after_reads_seconds_and_http_dates() -> None:
    assert retry_after_seconds("12", T0) == 12.0
    assert retry_after_seconds("Sun, 08 Nov 2026 07:05:30 GMT", T0) == 30.0
    assert retry_after_seconds("Sun, 08 Nov 2026 07:00:00 GMT", T0) == 0.0
    assert retry_after_seconds(None, T0) is None
    assert retry_after_seconds("soon", T0) is None


def test_no_retry_after_doubles_the_wait_per_refusal(tmp_path: Path) -> None:
    limited = SnapshotRateLimitedError("429")
    post = Script(limited, limited, limited, page([node(1)]))
    clock = Clock()
    assert run(tmp_path, post, clock).status == "written"
    assert clock.slept == [30.0, 60.0, 120.0]


def test_a_retry_after_past_the_budget_is_not_slept(tmp_path: Path) -> None:
    post = Script(SnapshotRateLimitedError("429", 2000.0))
    clock = Clock()
    result = run(tmp_path, post, clock, backoff_budget_s=1500)
    assert result.status == "failed"
    assert clock.slept == []
    assert "does not fit" in result.reason


# --- keeping the last good snapshot --------------------------------------------------------------


def seed(tmp_path: Path, at: datetime = T0) -> Clock:
    """One good full read at ``at`` (default 07:05Z)."""
    clock = Clock(at)
    assert run(tmp_path, Script(page([node(1), node(2)])), clock).status == "written"
    return clock


def test_a_limit_that_outlasts_the_budget_keeps_the_last_good_snapshot_and_its_age(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    before = (tmp_path / "linear-2026-11-08.json").read_text(encoding="utf-8")
    later = Clock(T0 + timedelta(hours=1))
    result = run(tmp_path, Script(SnapshotRateLimitedError("429", 5000.0)), later)
    assert result.status == "stale"
    assert result.age_s == 3600
    assert "3600s ago" in result.reason
    assert (tmp_path / "linear-2026-11-08.json").read_text(encoding="utf-8") == before
    state = state_of(tmp_path)
    assert state["last_good_read_at"] == "2026-11-08T07:05:00Z"
    assert state["last_attempt_at"] == "2026-11-08T08:05:00Z"
    assert (state["last_attempt_status"], state["age_s"]) == ("stale", 3600)


def test_stale_on_a_new_utc_day_copies_the_last_good_snapshot_to_todays_name(
    tmp_path: Path,
) -> None:
    seed(tmp_path, datetime(2026, 11, 8, 23, 5, tzinfo=UTC))
    next_day = Clock(datetime(2026, 11, 9, 0, 5, tzinfo=UTC))
    result = run(tmp_path, Script(SnapshotRateLimitedError("429", 5000.0)), next_day)
    assert (result.status, result.age_s) == ("stale", 3600)
    assert issues_in(tmp_path, "2026-11-09") == issues_in(tmp_path, "2026-11-08")


def test_a_kept_snapshot_older_than_the_limit_is_a_failure(tmp_path: Path) -> None:
    seed(tmp_path)
    much_later = Clock(T0 + timedelta(hours=7))
    result = run(tmp_path, Script(SnapshotRateLimitedError("429", 5000.0)), much_later)
    assert result.status == "failed"
    assert result.age_s == 7 * 3600
    assert "over the 21600s limit" in result.reason


def test_cli_exit_codes_stale_zero_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(tmp_path)
    serve(monkeypatch, response(429, headers={"Retry-After": "9999"}))
    monkeypatch.setattr(
        handler_module, "datetime", _FrozenDatetime(T0 + timedelta(hours=1))
    )
    assert main(["--out-dir", str(tmp_path), "--date", "2026-11-08"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("linear-snapshot | stale ")
    assert "age_s=3600" in out


class _FrozenDatetime:
    """Stands in for ``datetime`` in the handler module with a fixed ``now``."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self, tz: Any = None) -> datetime:
        return self._at

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime, name)


# --- reading what changed ------------------------------------------------------------------------


def test_the_next_read_asks_only_for_issues_updated_since_the_last_good_read(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    changed = [
        node(2, "2026-11-08T07:30:00.000Z", title="Ticket 2 renamed"),
        node(3, "2026-11-08T07:40:00.000Z"),
        node(
            1, "2026-11-08T07:41:00.000Z", state={"name": "Done", "type": "completed"}
        ),
    ]
    post = Script(page(changed))
    clock = Clock(T0 + timedelta(hours=1))
    result = run(tmp_path, post, clock)
    assert (result.status, result.mode, result.issue_count) == (
        "written",
        "incremental",
        2,
    )
    assert len(post.calls) == 1
    query, variables = post.calls[0]
    assert "updatedAt: { gt: $since }" in query
    assert (
        variables["since"] == "2026-11-08T06:55:00Z"
    )  # last good 07:05Z minus the 10 minute overlap
    docs = issues_in(tmp_path)
    assert set(docs) == {"OMN-2", "OMN-3"}
    assert docs["OMN-2"]["title"] == "Ticket 2 renamed"
    assert state_of(tmp_path)["last_good_read_at"] == "2026-11-08T08:05:00Z"
    assert state_of(tmp_path)["last_full_read_at"] == "2026-11-08T07:05:00Z"


@pytest.mark.parametrize(
    "gone",
    [
        {"assignee": {"id": OTHER}},
        {"assignee": None},
        {"archivedAt": "2026-11-08T07:50:00.000Z"},
        {"state": {"name": "Canceled", "type": "canceled"}},
    ],
)
def test_an_issue_that_left_the_open_list_is_dropped(
    tmp_path: Path, gone: dict[str, Any]
) -> None:
    seed(tmp_path)
    run(
        tmp_path,
        Script(page([node(1, "2026-11-08T07:50:00.000Z", **gone)])),
        Clock(T0 + timedelta(hours=1)),
    )
    assert set(issues_in(tmp_path)) == {"OMN-2"}


def test_the_incremental_read_follows_every_page(tmp_path: Path) -> None:
    seed(tmp_path)
    post = Script(
        page([node(3, "2026-11-08T07:40:00.000Z")], more=True),
        page([node(4, "2026-11-08T07:41:00.000Z")]),
    )
    run(tmp_path, post, Clock(T0 + timedelta(hours=1)), page_size=1)
    assert [v["after"] for _, v in post.calls] == [None, "c"]
    assert set(issues_in(tmp_path)) == {"OMN-1", "OMN-2", "OMN-3", "OMN-4"}


def test_a_full_read_is_made_again_once_the_last_one_is_a_day_old(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    post = Script(page([node(2)]))
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=24, minutes=1)))
    assert result.mode == "full"
    assert post.calls[0][1] == {"first": 100, "after": None}
    assert set(issues_in(tmp_path, "2026-11-09")) == {"OMN-2"}
    assert state_of(tmp_path)["last_full_read_at"] == "2026-11-09T07:06:00Z"


def test_a_changed_signed_in_user_forces_a_full_read(tmp_path: Path) -> None:
    seed(tmp_path)
    other = {
        "viewer": {"id": OTHER},
        "issues": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    full = {
        "viewer": {"id": OTHER},
        "issues": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [node(9, assignee={"id": OTHER})],
        },
    }
    post = Script(other, full)
    result = run(tmp_path, post, Clock(T0 + timedelta(hours=1)))
    assert result.mode == "full"
    assert set(issues_in(tmp_path)) == {"OMN-9"}


def test_a_429_in_the_middle_of_the_incremental_read_resumes_it(tmp_path: Path) -> None:
    seed(tmp_path)
    post = Script(
        SnapshotRateLimitedError("429", 20.0),
        page([node(3, "2026-11-08T07:40:00.000Z")]),
    )
    clock = Clock(T0 + timedelta(hours=1))
    result = run(tmp_path, post, clock)
    assert (result.status, result.mode) == ("written", "incremental")
    assert clock.slept == [20.0]
    assert len(post.calls) == 2


def test_a_failure_that_is_not_a_rate_limit_still_fails_and_is_recorded(
    tmp_path: Path,
) -> None:
    seed(tmp_path)
    result = run(tmp_path, Script(RuntimeError("boom")), Clock(T0 + timedelta(hours=1)))
    assert result.status == "failed"
    state = state_of(tmp_path)
    assert (state["last_attempt_status"], state["last_good_read_at"]) == (
        "failed",
        "2026-11-08T07:05:00Z",
    )
