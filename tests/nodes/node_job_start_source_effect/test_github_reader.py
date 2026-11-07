# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub reader of node_job_start_source_effect over an injected HTTP getter (OMN-22897):
golden read, pagination, and every way the read can fail, each typed and none a silent zero."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_job_start_source_effect.adapters.github_job_start_reader import (
    GithubJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.handlers.handler_job_start_source import (
    HandlerJobStartSource,
    load_topics,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStartRead,
    ModelJobStartRequest,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import ModelGithubSource
from tests.nodes.node_job_start_source_effect.support import (
    GROUP,
    NOW,
    REPO_A,
    REPO_B,
    TOKEN_ENV,
    TOKEN_VALUE,
    FakeHttp,
    Route,
    job_item,
    json_route,
    overlay,
    run_item,
)

pytestmark = pytest.mark.unit

SINCE = NOW - timedelta(seconds=3600)


def _source(**changes: object) -> ModelGithubSource:
    fields = overlay("overlay.github.example.yaml")
    assert isinstance(fields.source, ModelGithubSource)
    return fields.source.model_copy(update=changes)


def _read(
    http: FakeHttp,
    source: ModelGithubSource | None = None,
    *,
    token: str | None = TOKEN_VALUE,
    repositories: tuple[str, ...] = (REPO_A, REPO_B),
    lookback: int = 86400,
) -> ModelJobStartRead:
    environ = {TOKEN_ENV: token} if token is not None else {}
    return GithubJobStartReader(
        source or _source(), http, environ, run_lookback_seconds=lookback
    ).read(repositories=repositories, since=SINCE, until=NOW, now=NOW)


def _routes(
    runs_a: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]
) -> dict[str, Route]:
    routes: dict[str, Route] = {
        f"/repos/{REPO_A}/actions/runs": json_route(
            {"total_count": len(runs_a), "workflow_runs": runs_a}
        ),
        f"/repos/{REPO_B}/actions/runs": json_route(
            {"total_count": 0, "workflow_runs": []}
        ),
    }
    for run_id, items in jobs.items():
        routes[f"/repos/{REPO_A}/actions/runs/{run_id}/jobs"] = json_route(
            {"total_count": len(items), "jobs": items}
        )
    return routes


def _http(
    runs_a: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]
) -> FakeHttp:
    return FakeHttp(_routes(runs_a, jobs))


# -- golden chain ----------------------------------------------------------------------------


def test_golden_chain_the_reader_returns_the_jobs_that_started_in_the_window() -> None:
    http = _http(
        [run_item(1001), run_item(1002)],
        {
            1001: [
                job_item(1, 1001, started_at="2026-11-07T11:10:00Z"),
                # started before the window opened: a run that was queued long, not a start of this window
                job_item(2, 1001, started_at="2026-11-07T09:00:00Z"),
                # queued, not started: no start time
                job_item(3, 1001, started_at=None),
            ],
            1002: [job_item(4, 1002, started_at="2026-11-07T11:50:00Z", group=None)],
        },
    )
    read = _read(http)
    assert read.read_failure is None
    assert read.repositories_read == (REPO_A, REPO_B)
    assert [s.job_id for s in read.starts] == [1, 4]
    first = read.starts[0]
    assert (first.repository, first.run_id, first.runner_group_name) == (
        REPO_A,
        1001,
        GROUP,
    )
    assert first.started_at.isoformat() == "2026-11-07T11:10:00+00:00"
    assert first.run_event == "push"
    assert first.head_is_fork is False
    assert first.repository_private is True
    assert read.starts[1].runner_group_name is None
    # 2 run listings + 2 job listings
    assert read.calls == 4
    assert len(http.calls) == 4


def test_golden_chain_a_run_last_updated_before_the_window_is_not_asked_for_its_jobs() -> (
    None
):
    http = _http([run_item(1001, updated_at="2026-11-07T08:00:00Z")], {1001: []})
    read = _read(http)
    assert read.read_failure is None
    assert read.starts == ()
    assert all("/jobs" not in path for path, _, _ in http.calls)


def test_golden_chain_a_fork_run_is_marked_from_its_head_repository() -> None:
    forked = run_item(
        1001,
        head_repository={"full_name": "other-org/example-repo"},
        event="pull_request",
    )
    read = _read(
        _http([forked], {1001: [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")]})
    )
    assert read.starts[0].head_is_fork is True
    assert read.starts[0].run_event == "pull_request"


def test_golden_chain_the_run_listing_asks_for_the_lookback_the_overlay_sets() -> None:
    http = _http([], {})
    _read(http, lookback=7200)
    path, query, _ = http.calls[0]
    assert path == f"/repos/{REPO_A}/actions/runs"
    assert query["created"] == ">=2026-11-07T09:00:00Z"
    assert query["per_page"] == str(_source().page_size)


def test_golden_chain_pages_are_followed_up_to_the_overlays_limit() -> None:
    source = _source(page_size=2, max_pages_per_repository=3)
    pages = {
        "1": [run_item(1), run_item(2)],
        "2": [run_item(3)],
    }
    http = FakeHttp(
        {
            f"/repos/{REPO_A}/actions/runs": lambda q: (
                200,
                json_route({"workflow_runs": pages.get(q["page"], [])})(q)[1],
            ),
            f"/repos/{REPO_B}/actions/runs": json_route({"workflow_runs": []}),
            **{
                f"/repos/{REPO_A}/actions/runs/{i}/jobs": json_route(
                    {"jobs": [job_item(i * 10, i, started_at="2026-11-07T11:10:00Z")]}
                )
                for i in (1, 2, 3)
            },
        }
    )
    read = _read(http, source)
    assert read.read_failure is None
    assert [s.job_id for s in read.starts] == [10, 20, 30]


def test_golden_chain_the_handler_runs_the_github_reader_end_to_end() -> None:
    fields = overlay("overlay.github.example.yaml")
    http = _http(
        [run_item(1001)], {1001: [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")]}
    )
    result = HandlerJobStartSource(
        overlay=fields, http_get=http, environ={TOKEN_ENV: TOKEN_VALUE}
    ).handle(ModelJobStartRequest(correlation_id="c-1", now=NOW))
    assert result.backend == "github"
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.job_start_count == 1
    assert result.calls == 3


# -- error chain -----------------------------------------------------------------------------


def test_error_chain_a_429_is_rate_limited() -> None:
    http = FakeHttp({f"/repos/{REPO_A}/actions/runs": json_route({}, status=429)})
    read = _read(http)
    assert read.read_failure is EnumJobStartFailure.RATE_LIMITED
    assert read.failed_repository == REPO_A
    assert read.starts == ()


def test_error_chain_a_403_that_names_the_rate_limit_is_rate_limited() -> None:
    http = FakeHttp(
        {
            f"/repos/{REPO_A}/actions/runs": json_route(
                {"message": "API rate limit exceeded for installation"}, status=403
            )
        }
    )
    assert _read(http).read_failure is EnumJobStartFailure.RATE_LIMITED


@pytest.mark.parametrize("status", [401, 403, 404])
def test_error_chain_a_denied_listing_is_org_read_denied(status: int) -> None:
    http = FakeHttp({f"/repos/{REPO_A}/actions/runs": json_route({}, status=status)})
    read = _read(http)
    assert read.read_failure is EnumJobStartFailure.ORG_READ_DENIED
    assert read.failed_repository == REPO_A


def test_error_chain_a_missing_token_is_denied_and_makes_no_call() -> None:
    http = _http([], {})
    read = _read(http, token=None)
    assert read.read_failure is EnumJobStartFailure.ORG_READ_DENIED
    assert read.calls == 0
    assert http.calls == []


@pytest.mark.parametrize("status", [500, 502, 599])
def test_error_chain_a_server_error_is_unreadable(status: int) -> None:
    http = FakeHttp({f"/repos/{REPO_A}/actions/runs": json_route({}, status=status)})
    assert _read(http).read_failure is EnumJobStartFailure.UNREADABLE


def test_error_chain_a_body_that_is_not_json_is_unreadable() -> None:
    http = FakeHttp({f"/repos/{REPO_A}/actions/runs": lambda _q: (200, b"<html>")})
    assert _read(http).read_failure is EnumJobStartFailure.UNREADABLE


def test_error_chain_a_run_without_an_id_is_unreadable_not_skipped() -> None:
    http = _http([{"event": "push"}], {})
    assert _read(http).read_failure is EnumJobStartFailure.UNREADABLE


def test_error_chain_a_job_listing_that_fails_stops_the_read() -> None:
    routes = _routes([run_item(1001)], {})
    routes[f"/repos/{REPO_A}/actions/runs/1001/jobs"] = json_route({}, status=502)
    read = _read(FakeHttp(routes))
    assert read.read_failure is EnumJobStartFailure.UNREADABLE
    assert read.starts == ()
    assert read.repositories_read == ()


def test_error_chain_the_call_past_the_budget_is_not_made() -> None:
    http = _http(
        [run_item(1001), run_item(1002)],
        {
            1001: [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")],
            1002: [job_item(2, 1002, started_at="2026-11-07T11:10:00Z")],
        },
    )
    read = _read(http, _source(max_calls=2))
    assert read.read_failure is EnumJobStartFailure.BUDGET_EXHAUSTED
    assert read.calls == 2
    assert len(http.calls) == 2


def test_error_chain_more_pages_than_the_limit_is_a_truncated_window_not_a_short_one() -> (
    None
):
    source = _source(page_size=2, max_pages_per_repository=1)
    http = FakeHttp(
        {
            f"/repos/{REPO_A}/actions/runs": json_route(
                {"workflow_runs": [run_item(1), run_item(2)]}
            ),
            f"/repos/{REPO_A}/actions/runs/1/jobs": json_route({"jobs": []}),
            f"/repos/{REPO_A}/actions/runs/2/jobs": json_route({"jobs": []}),
        }
    )
    read = _read(http, source)
    assert read.read_failure is EnumJobStartFailure.WINDOW_TRUNCATED
    assert read.failed_repository == REPO_A


def test_error_chain_a_rate_limited_read_ends_in_the_failure_topic() -> None:
    fields = overlay("overlay.github.example.yaml")
    http = FakeHttp({f"/repos/{REPO_A}/actions/runs": json_route({}, status=429)})
    result = HandlerJobStartSource(
        overlay=fields, http_get=http, environ={TOKEN_ENV: TOKEN_VALUE}
    ).handle(ModelJobStartRequest(correlation_id="c-2", now=NOW))
    assert result.read_failure is EnumJobStartFailure.RATE_LIMITED
    assert result.job_start_count is None
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert HandlerJobStartSource.event_for(result)[0] == load_topics()["read_failed"]


# -- the token ---------------------------------------------------------------------------------


def test_the_token_goes_only_in_the_authorization_header_and_in_no_result() -> None:
    fields = overlay("overlay.github.example.yaml")
    http = _http(
        [run_item(1001)], {1001: [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")]}
    )
    result = HandlerJobStartSource(
        overlay=fields, http_get=http, environ={TOKEN_ENV: TOKEN_VALUE}
    ).handle(ModelJobStartRequest(correlation_id="c-3", now=NOW))
    assert http.calls
    for path, query, headers in http.calls:
        assert headers["Authorization"] == f"Bearer {TOKEN_VALUE}"
        assert TOKEN_VALUE not in path
        assert TOKEN_VALUE not in "".join(query.values())
    assert TOKEN_VALUE not in result.model_dump_json()
    assert TOKEN_VALUE not in fields.model_dump_json()
