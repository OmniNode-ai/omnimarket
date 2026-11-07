# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_job_start_source_effect: golden chain, error chain, the named injected breaks and the
retained store (OMN-22897)."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_job_start_source_effect.adapters.inmemory_job_start_reader import (
    InMemoryJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.adapters.job_start_store import (
    InMemoryJobStartStore,
    LedgerJobStartStore,
)
from omnimarket.nodes.node_job_start_source_effect.handlers.handler_job_start_source import (
    HandlerJobStartSource,
    load_topics,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStartRead,
    ModelJobStartRequest,
    ModelJobStartResult,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    ModelInMemorySource,
    ModelLedgerStore,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.nodes.node_job_start_source_effect.support import (
    GROUP,
    NODE_DIR,
    NOW,
    REPO_A,
    REPO_B,
    FakeReader,
    failed_read,
    good_read,
    overlay,
    start,
)

pytestmark = pytest.mark.unit


def _request() -> ModelJobStartRequest:
    return ModelJobStartRequest(correlation_id="job-start-1", now=NOW)


def _run(
    read: ModelJobStartRead | None = None,
    *,
    raises: BaseException | None = None,
    store: Any = None,
    name: str = "overlay.example.yaml",
) -> tuple[ModelJobStartResult, FakeReader]:
    reader = FakeReader(read, raises)
    handler = HandlerJobStartSource(
        overlay=overlay(name), reader=reader, store=store or InMemoryJobStartStore()
    )
    return handler.handle(_request()), reader


def _subject(result: ModelJobStartResult, name: str) -> Any:
    return next(s for s in result.reading.subjects if s.name == name)


# -- golden chain ----------------------------------------------------------------------------


def test_golden_chain_a_clean_read_keeps_each_start_with_its_time_group_and_run() -> (
    None
):
    first = start(job_id=1, run_id=11, started_at="2026-11-07T11:10:00Z")
    second = start(
        repository=REPO_B, job_id=2, run_id=12, started_at="2026-11-07T11:20:00Z"
    )
    store = InMemoryJobStartStore()
    result, reader = _run(good_read(second, first), store=store)
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.read_failure is None
    assert result.job_start_count == 2
    # ordered by the time the job started, whatever order the reader returned
    assert [s.job_id for s in result.starts] == [1, 2]
    assert result.starts[0].started_at.isoformat() == "2026-11-07T11:10:00+00:00"
    assert result.starts[0].runner_group_name == GROUP
    assert (result.starts[0].repository, result.starts[0].run_id) == (REPO_A, 11)
    assert result.repositories_read == (REPO_A, REPO_B)
    assert result.store is not None
    assert result.store.status == "written"
    assert store.results == [result.model_copy(update={"store": None})]
    # the window and the repository list come from the overlay, nothing is hard-coded
    request = reader.requests[0]
    assert request["repositories"] == (REPO_A, REPO_B)
    assert (request["until"] - request["since"]).total_seconds() == 3600


def test_golden_chain_a_read_with_no_start_in_the_window_is_a_working_zero() -> None:
    result, _ = _run(good_read())
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.job_start_count == 0


def test_golden_chain_the_terminal_event_is_the_completed_topic() -> None:
    result, _ = _run(good_read(start()))
    topic, body = HandlerJobStartSource.event_for(result)
    assert topic == load_topics()["read_completed"]
    assert body["job_start_count"] == 1
    assert body["reading"]["verdict"] == "WORKING"
    assert "starts" not in body


def test_golden_chain_the_in_memory_reader_returns_the_overlays_fixture() -> None:
    fields = overlay()
    assert isinstance(fields.source, ModelInMemorySource)
    handler = HandlerJobStartSource(overlay=fields)
    result = handler.handle(_request())
    assert result.backend == "inmemory"
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.job_start_count == len(fields.source.fixture.starts)
    assert result.job_start_count >= 1
    assert result.store is not None
    assert result.store.status == "written"


def test_golden_chain_the_same_run_again_is_already_written() -> None:
    store = InMemoryJobStartStore()
    first, _ = _run(good_read(start()), store=store)
    second, _ = _run(good_read(start()), store=store)
    assert first.row_key == second.row_key
    assert second.store is not None
    assert second.store.status == "already-written"
    assert len(store.results) == 1


# -- the named injected breaks: the reading must fail for real -------------------------------


@pytest.mark.parametrize(
    ("break_name", "kwargs"),
    [
        ("fork_head_on_watched_group", {"fork": True, "private": True}),
        ("public_repository_on_watched_group", {"fork": False, "private": False}),
    ],
)
def test_injected_break_a_public_start_on_a_watched_group_reads_not_working(
    break_name: str, kwargs: dict[str, Any]
) -> None:
    control, _ = _run(good_read(start(job_id=1)))
    assert control.reading.verdict is EnumReadingVerdict.WORKING
    broken, _ = _run(good_read(start(job_id=1), start(job_id=2, run_id=2002, **kwargs)))
    assert broken.reading.verdict is EnumReadingVerdict.NOT_WORKING
    subject = _subject(broken, "js_public_starts_off_baseline")
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1
    # the other subjects still read what they read
    assert (
        _subject(broken, "js_repositories_read").verdict is EnumReadingVerdict.WORKING
    )
    # a NOT_WORKING reading was read: it is a reading, not a failure of the run
    assert broken.read_failure is None
    assert HandlerJobStartSource.event_for(broken)[0] == load_topics()["read_completed"]


def test_injected_break_a_public_start_off_every_watched_group_is_not_counted() -> None:
    result, _ = _run(good_read(start(group="example-hosted-group", fork=True)))
    assert result.reading.verdict is EnumReadingVerdict.WORKING


def test_injected_break_a_public_start_of_a_baseline_repository_is_not_counted() -> (
    None
):
    fields = overlay(baseline_public_repositories=[REPO_B])
    handler = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(good_read(start(repository=REPO_B, private=False))),
    )
    assert handler.handle(_request()).reading.verdict is EnumReadingVerdict.WORKING
    # a fork start is never on the baseline, whatever its repository
    forked = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(
            good_read(start(repository=REPO_B, private=False, fork=True))
        ),
    )
    assert forked.handle(_request()).reading.verdict is EnumReadingVerdict.NOT_WORKING


def test_injected_break_a_repository_the_reader_drops_reads_unknown_not_zero() -> None:
    dropped = ModelJobStartRead(repositories_read=(REPO_A,), read_at=NOW, calls=1)
    result, _ = _run(dropped)
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.read_failure is EnumJobStartFailure.REPOSITORIES_INCOMPLETE
    assert result.failed_repository == REPO_B
    assert result.job_start_count is None
    assert HandlerJobStartSource.event_for(result)[0] == load_topics()["read_failed"]


def test_injected_break_a_stale_read_reads_unknown() -> None:
    result, _ = _run(good_read(start(), age_seconds=1801))
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert _subject(result, "js_read_age").reason == "read-stale"
    assert _subject(result, "js_public_starts_off_baseline").verdict is (
        EnumReadingVerdict.UNKNOWN
    )
    fresh, _ = _run(good_read(start(), age_seconds=1800))
    assert fresh.reading.verdict is EnumReadingVerdict.WORKING


# -- error chain: a failed read is a typed failure event, never a silent zero ----------------


@pytest.mark.parametrize(
    "failure",
    [
        EnumJobStartFailure.RATE_LIMITED,
        EnumJobStartFailure.ORG_READ_DENIED,
        EnumJobStartFailure.BUDGET_EXHAUSTED,
        EnumJobStartFailure.UNREADABLE,
        EnumJobStartFailure.WINDOW_TRUNCATED,
    ],
)
def test_error_chain_a_failed_read_is_a_typed_failure_event(
    failure: EnumJobStartFailure,
) -> None:
    store = InMemoryJobStartStore()
    result, _ = _run(failed_read(failure), store=store)
    assert result.read_failure is failure
    assert result.failed_repository == REPO_A
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.job_start_count is None
    assert all(s.verdict is EnumReadingVerdict.UNKNOWN for s in result.reading.subjects)
    topic, body = HandlerJobStartSource.event_for(result)
    assert topic == load_topics()["read_failed"]
    assert body["read_failure"] == failure.value
    assert body["job_start_count"] is None
    # the failed read is still kept, as an unread record that holds no starts
    assert len(store.results) == 1
    assert store.results[0].read_failure is failure


def test_error_chain_a_reader_that_raises_is_a_typed_failure_not_a_crash() -> None:
    result, _ = _run(raises=RuntimeError("boom"))
    assert result.read_failure is EnumJobStartFailure.READER_RAISED
    assert result.job_start_count is None
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert HandlerJobStartSource.event_for(result)[0] == load_topics()["read_failed"]
    assert "boom" not in result.model_dump_json()


def test_error_chain_a_reader_that_raises_a_typed_error_keeps_its_failure() -> None:
    from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
        JobStartReaderError,
    )

    result, _ = _run(
        raises=JobStartReaderError(EnumJobStartFailure.RATE_LIMITED, REPO_B, calls=4)
    )
    assert result.read_failure is EnumJobStartFailure.RATE_LIMITED
    assert result.failed_repository == REPO_B
    assert result.calls == 4


def test_error_chain_the_in_memory_fixture_can_inject_a_failure() -> None:
    fields = overlay()
    assert isinstance(fields.source, ModelInMemorySource)
    fixture = fields.source.fixture.model_copy(
        update={"starts": (), "read_failure": EnumJobStartFailure.RATE_LIMITED}
    )
    result = HandlerJobStartSource(
        overlay=fields, reader=InMemoryJobStartReader(fixture)
    ).handle(_request())
    assert result.read_failure is EnumJobStartFailure.RATE_LIMITED
    assert result.job_start_count is None


# -- the retained store ----------------------------------------------------------------------


def _cells(row: str) -> dict[str, str]:
    parts = [p.strip() for p in row.strip().split(" | ")]
    return dict(p.split("=", 1) for p in parts[2:] if "=" in p)


def _ledger_store(
    ledger: Any, name: str = "overlay.ledger.example.yaml"
) -> tuple[LedgerJobStartStore, Any]:
    fields = overlay(name)
    assert isinstance(fields.store, ModelLedgerStore)
    return LedgerJobStartStore(ledger, fields.store, "example-host"), fields


def test_golden_chain_the_ledger_store_appends_one_status_row_per_read() -> None:
    ledger = InMemoryLedger()
    store, fields = _ledger_store(ledger)
    handler = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(
            good_read(
                start(job_id=1, started_at="2026-11-07T11:10:00Z"),
                start(job_id=2, started_at="2026-11-07T11:40:00Z"),
            )
        ),
        store=store,
    )
    result = handler.handle(_request())
    assert result.store is not None
    assert result.store.status == "written"
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert row.split(" | ")[1] == "STATUS"
    cells = _cells(row)
    assert cells["src"] == "job-start-source"
    assert cells["lane"] == "job-start-source"
    assert cells["verdict"] == "WORKING"
    assert cells["outcome"] == EnumReadingOutcome.READ.value
    assert cells["job_starts"] == "2"
    assert cells["first_started_at"] == "2026-11-07T11:10:00Z"
    assert cells["last_started_at"] == "2026-11-07T11:40:00Z"
    assert cells["host"] == "example-host"
    assert cells["key"] == result.row_key
    assert len(cells["starts_digest"]) >= 16
    # the same read again lands on the same request id and is not written twice
    again = handler.handle(_request())
    assert again.store is not None
    assert again.store.status == "already-written"
    assert len(ledger.lines) == 1


def test_injected_break_the_ledger_row_of_a_public_start_names_the_run() -> None:
    ledger = InMemoryLedger()
    store, fields = _ledger_store(ledger)
    result = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(good_read(start(job_id=9, run_id=777, fork=True))),
        store=store,
    ).handle(_request())
    cells = _cells(ledger.lines[0])
    assert result.reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert cells["verdict"] == "NOT_WORKING"
    assert cells["js_public_starts_off_baseline"] == "NOT_WORKING"
    assert cells["public_off_baseline_runs"] == "777"


def test_error_chain_a_failed_read_writes_an_unknown_row_with_no_start_count() -> None:
    ledger = InMemoryLedger()
    store, fields = _ledger_store(ledger)
    result = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(failed_read(EnumJobStartFailure.RATE_LIMITED)),
        store=store,
    ).handle(_request())
    cells = _cells(ledger.lines[0])
    assert cells["verdict"] == "UNKNOWN"
    assert cells["outcome"] == EnumReadingOutcome.SOURCE_UNREADABLE.value
    assert cells["reason"] == "rate-limited"
    assert "job_starts" not in cells
    assert result.store is not None
    assert result.store.status == "written"


@pytest.mark.parametrize("status", ["refused", "error", "pending"])
def test_error_chain_an_append_that_does_not_land_fails_the_run(status: str) -> None:
    ledger = InMemoryLedger()
    ledger.append_status = status
    store, fields = _ledger_store(ledger)
    result = HandlerJobStartSource(
        overlay=fields, reader=FakeReader(good_read(start())), store=store
    ).handle(_request())
    assert result.store is not None
    assert result.store.failed
    assert result.store.status == "failed"
    assert HandlerJobStartSource.event_for(result)[0] == load_topics()["store_failed"]


def test_error_chain_a_ledger_store_with_no_appender_fails_the_run() -> None:
    fields = overlay("overlay.ledger.example.yaml")
    assert isinstance(fields.store, ModelLedgerStore)
    result = HandlerJobStartSource(
        overlay=fields,
        reader=FakeReader(good_read(start())),
        environ={fields.store.ledger_append.host_env: "example-host"},
    ).handle(_request())
    assert result.store is not None
    assert result.store.failed
    assert "no-appender" in result.store.detail


def test_the_appender_receives_the_row_as_text_with_a_stable_request_id() -> None:
    class _Appender:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
            self.calls.append((rows, request_id))
            return ModelAppendOutcome(status="accepted")

    appender = _Appender()
    store, fields = _ledger_store(appender)
    handler = HandlerJobStartSource(
        overlay=fields, reader=FakeReader(good_read(start())), store=store
    )
    handler.handle(_request())
    handler.handle(_request())
    assert appender.calls[0][1] == appender.calls[1][1]
    assert appender.calls[0][0].endswith("\n")


# -- the contract ----------------------------------------------------------------------------


def test_the_contract_declares_the_topics_the_handler_reads() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    topics = load_topics()
    assert set(topics) == {
        "read_requested",
        "read_completed",
        "read_failed",
        "store_failed",
    }
    assert contract["terminal_event"] == topics["read_completed"]
    for topic in topics.values():
        assert topic.startswith(("onex.cmd.omnimarket.", "onex.evt.omnimarket."))
        assert topic.endswith(".v1")
    published = set(contract["externally_consumed_topics"])
    assert {
        topics["read_completed"],
        topics["read_failed"],
        topics["store_failed"],
    } <= published
    assert contract["descriptor"]["side_effects"] == [
        "network-read",
        "retained-store-append",
    ]
    assert contract["handler"]["class"] == "HandlerJobStartSource"
