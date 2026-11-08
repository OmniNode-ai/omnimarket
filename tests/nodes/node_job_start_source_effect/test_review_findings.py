# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5275:1 to 5275:6 on node_job_start_source_effect (OMN-22897); each failed before the fix."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_job_start_source_effect.adapters.github_job_start_reader import (
    GithubJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.adapters.inmemory_job_start_reader import (
    InMemoryJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.adapters.job_start_store import (
    InMemoryJobStartStore,
    LedgerJobStartStore,
)
from omnimarket.nodes.node_job_start_source_effect.handlers.handler_job_start_source import (
    HandlerJobStartSource,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStartRequest,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    ModelGithubSource,
    ModelInMemoryFixture,
    ModelLedgerStore,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.nodes.node_job_start_source_effect.support import (
    NOW,
    REPO_A,
    REPO_B,
    TOKEN_ENV,
    TOKEN_VALUE,
    FakeHttp,
    FakeReader,
    declared_timeout_seconds,
    good_read,
    job_item,
    json_route,
    overlay,
    run_item,
    start,
)

pytestmark = pytest.mark.unit

SINCE = NOW - timedelta(seconds=3600)


def _github(http: FakeHttp, **changes: Any) -> Any:
    fields = overlay("overlay.github.example.yaml")
    assert isinstance(fields.source, ModelGithubSource)
    source = fields.source.model_copy(update=changes)
    return GithubJobStartReader(
        source,
        http,
        {TOKEN_ENV: TOKEN_VALUE},
        run_lookback_seconds=86400,
        operation_timeout_seconds=declared_timeout_seconds(),
    ).read(repositories=(REPO_A,), since=SINCE, until=NOW, now=NOW)


def _routes(runs: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> FakeHttp:
    return FakeHttp(
        {
            f"/repos/{REPO_A}/actions/runs": json_route(
                {"total_count": len(runs), "workflow_runs": runs}
            ),
            f"/repos/{REPO_A}/actions/runs/1001/jobs": json_route(
                {"total_count": len(jobs), "jobs": jobs}
            ),
        }
    )


def test_5275_1_the_jobs_listing_covers_every_attempt_of_a_rerun() -> None:
    http = _routes(
        [run_item(1001)],
        [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")],
    )
    _github(http)
    job_queries = [q for path, q, _ in http.calls if path.endswith("/jobs")]
    assert job_queries
    assert all(q["filter"] == "all" for q in job_queries)


def test_5275_5_an_exactly_full_final_page_is_a_complete_read_when_total_count_says_so() -> (
    None
):
    jobs = [
        job_item(1, 1001, started_at="2026-11-07T11:10:00Z"),
        job_item(2, 1001, started_at="2026-11-07T11:20:00Z"),
    ]
    http = _routes([run_item(1001)], jobs)
    read = _github(http, page_size=2, max_pages_per_repository=1)
    assert read.read_failure is None, read.read_failure
    assert [s.job_id for s in read.starts] == [1, 2]


def test_5275_5_control_a_listing_with_more_than_the_pages_allowed_is_still_truncated() -> (
    None
):
    http = FakeHttp(
        {
            f"/repos/{REPO_A}/actions/runs": json_route(
                {"total_count": 5, "workflow_runs": [run_item(1001), run_item(1002)]}
            )
        }
    )
    read = _github(http, page_size=2, max_pages_per_repository=1)
    assert read.read_failure is EnumJobStartFailure.WINDOW_TRUNCATED


def test_5275_6_a_run_that_names_no_head_repository_is_treated_as_a_fork() -> None:
    run = run_item(1001)
    run["head_repository"] = None
    http = _routes([run], [job_item(1, 1001, started_at="2026-11-07T11:10:00Z")])
    read = _github(http)
    assert [s.head_is_fork for s in read.starts] == [True]


def test_5275_4_the_in_memory_reader_keeps_to_the_requested_repositories_and_window() -> (
    None
):
    inside = start(job_id=1, started_at="2026-11-07T11:30:00Z")
    too_old = start(job_id=2, started_at="2026-11-07T01:00:00Z")
    too_new = start(job_id=3, started_at="2026-11-07T13:00:00Z")
    other = start(job_id=4, repository=REPO_B, started_at="2026-11-07T11:30:00Z")
    reader = InMemoryJobStartReader(
        ModelInMemoryFixture(
            starts=(inside, too_old, too_new, other), read_age_seconds=0
        )
    )
    read = reader.read(repositories=(REPO_A,), since=SINCE, until=NOW, now=NOW)
    assert [s.job_id for s in read.starts] == [1]


def test_5275_2_two_reads_in_one_second_with_different_windows_keep_distinct_keys() -> (
    None
):
    fields = overlay()
    store = InMemoryJobStartStore()
    handler = HandlerJobStartSource(
        overlay=fields, reader=FakeReader(good_read(start())), store=store
    )
    first = handler.handle(
        ModelJobStartRequest(correlation_id="a", now=NOW.replace(microsecond=100))
    )
    second = handler.handle(
        ModelJobStartRequest(correlation_id="b", now=NOW.replace(microsecond=900000))
    )
    assert first.row_key != second.row_key
    assert second.store is not None
    assert second.store.status == "written"
    retry = handler.handle(
        ModelJobStartRequest(correlation_id="c", now=NOW.replace(microsecond=100))
    )
    assert retry.row_key == first.row_key


def test_5275_3_changed_contents_under_one_row_key_keep_one_ledger_request_id() -> None:
    class Appender:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
            self.ids.append(request_id)
            return ModelAppendOutcome(status="accepted")

    appender = Appender()
    fields = overlay("overlay.ledger.example.yaml")
    assert isinstance(fields.store, ModelLedgerStore)
    store = LedgerJobStartStore(appender, fields.store, "example-host")
    request = ModelJobStartRequest(correlation_id="a", now=NOW)
    for read in (good_read(start()), good_read(start(), start(job_id=5002))):
        HandlerJobStartSource(
            overlay=fields, reader=FakeReader(read), store=store
        ).handle(request)
    assert len(appender.ids) == 2
    assert appender.ids[0] == appender.ids[1]
