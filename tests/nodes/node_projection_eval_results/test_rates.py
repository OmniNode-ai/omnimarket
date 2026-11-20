# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171 AC3: rates are recounts of first tries, and a close shows no rate until all its attempts are in."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.handlers.stats_wilson import wilson_interval
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_SNAPSHOT,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    judged_event,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
CALL_1 = "00000000-0000-0000-0000-00000000c001"
CALL_2 = "00000000-0000-0000-0000-00000000c002"


def _rate(store: MemoryStore, band: str = "all") -> Any:
    (row,) = [r for r in store.rates if r.size_band == band]
    return row


def _snapshot_runs(published: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    assert published, "nothing was published"
    topic, snapshot = published[-1]
    assert topic == TOPIC_SNAPSHOT
    return snapshot["models"][0]["runs"]


async def test_a_task_with_three_tries_counts_once() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    tries = [
        try_row(RUN, "t1", ordinal=1, verdict="fail_check"),
        try_row(RUN, "t1", ordinal=2, verdict="pass"),
        try_row(RUN, "t1", ordinal=3, verdict="pass"),
    ]
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, *tries), at(5))
    assert len(store.items) == 3
    row = _rate(store)
    assert (row.n, row.passed) == (1, 0)


async def test_one_item_delivered_twice_with_different_times_keeps_n_at_one() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    event = code_event(RUN, try_row(RUN, "t1", verdict="pass"))
    await deliver(writer, TOPIC_CODE_TRY, event, at(5))
    await deliver(writer, TOPIC_CODE_TRY, event, at(8))
    assert _rate(store).n == 1


async def test_a_first_try_unscorable_for_infrastructure_then_passed_on_retry_counts_as_one_pass() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t1", verdict="unscorable", source="env")),
        at(5),
    )
    held = _rate(store)
    assert (held.n, held.unscorable_infrastructure) == (0, 1)
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t1", verdict="pass")),
        at(9),
    )
    row = _rate(store)
    assert (row.n, row.passed, row.unscorable_infrastructure) == (1, 1, 0)


async def test_an_infrastructure_unscorable_try_is_outside_n_and_a_model_class_one_counts_as_not_passed() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    rows = [
        try_row(RUN, "t1", verdict="pass"),
        try_row(RUN, "t2", verdict="unscorable", source="harness"),
        try_row(RUN, "t3", verdict="unscorable", source="draft_not_recoverable"),
    ]
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, *rows), at(5))
    row = _rate(store)
    assert (row.n, row.passed) == (2, 1)
    assert (row.unscorable_infrastructure, row.unscorable_model) == (1, 1)
    assert (row.wilson_low, row.wilson_high) == wilson_interval(1, 2)


async def test_sizes_are_counted_apart_and_together() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    rows = [
        try_row(RUN, "t1", verdict="pass", band="S"),
        try_row(RUN, "t2", verdict="fail_check", band="S"),
        try_row(RUN, "t3", verdict="pass", band="M"),
    ]
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, *rows), at(5))
    assert {r.size_band: (r.n, r.passed) for r in store.rates} == {
        "S": (2, 1),
        "M": (1, 1),
        "all": (3, 2),
    }


async def test_two_revisions_and_settings_never_share_a_row() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    rows = [
        try_row(RUN, "t1", revision="sha256:rev-a"),
        try_row(RUN, "t2", revision="sha256:rev-b"),
        try_row(
            RUN, "t3", revision="sha256:rev-b", generation_settings_hash="settings-2"
        ),
    ]
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, *rows), at(5))
    keys = {
        (r.model_revision, r.generation_settings_hash)
        for r in store.rates
        if r.size_band == "all"
    }
    assert len(keys) == 3


_SEQUENCES = (("t1", MODEL, "S"), ("t2", MODEL, "S"))


async def test_close_shows_no_rate_until_the_last_event() -> None:
    """Close before the items: closing, with no snapshot rate, until the last first try is in."""
    store = MemoryStore()
    writer, published = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, close_event(RUN, _SEQUENCES), at(1))
    assert _snapshot_runs(published) == []
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t1", verdict="pass")),
        at(2),
    )
    assert {r.run_state for r in store.rates} == {EnumEvalRunState.CLOSING}
    assert _snapshot_runs(published) == []
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t2", verdict="fail_check")),
        at(3),
    )
    assert {r.run_state for r in store.rates} == {EnumEvalRunState.CLOSED}
    (run,) = _snapshot_runs(published)
    assert run["state"] == "closed"
    rates = {r["size_band"]: (r["n"], r["passed"]) for r in run["rates"]}
    assert rates == {"S": (2, 1), "all": (2, 1)}


async def test_close_then_an_infrastructure_unscorable_first_try_then_its_retry() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, close_event(RUN, _SEQUENCES), at(1))
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t1", verdict="pass")),
        at(2),
    )
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t2", verdict="unscorable", source="env")),
        at(3),
    )
    assert {r.run_state for r in store.rates} == {EnumEvalRunState.CLOSING}
    assert _snapshot_runs(published) == []
    await deliver(
        writer,
        TOPIC_CODE_TRY,
        code_event(RUN, try_row(RUN, "t2", verdict="pass")),
        at(4),
    )
    (run,) = _snapshot_runs(published)
    assert run["state"] == "closed"
    assert {r["size_band"]: (r["n"], r["passed"]) for r in run["rates"]}["all"] == (
        2,
        2,
    )


async def test_a_run_reads_closed_only_over_its_registered_tasks() -> None:
    store = MemoryStore()
    writer, published = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, close_event(RUN, _SEQUENCES), at(1))
    rows = [try_row(RUN, "t1"), try_row(RUN, "other-task")]
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, *rows), at(2))
    states = {(r.run_state, r.void_reason) for r in store.rates}
    assert states == {(EnumEvalRunState.VOID, "sample_mismatch")}
    (run,) = _snapshot_runs(published)
    assert (run["state"], run["void_reason"], run["rates"]) == (
        "void",
        "sample_mismatch",
        [],
    )


async def test_a_live_call_judged_twice_is_one_item_and_a_week_is_one_row_per_model() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    first = judged_event(CALL_1, accept=True, judged_at=at(2), rubric="v1")
    again = judged_event(CALL_1, accept=False, judged_at=at(9), rubric="v2")
    other = judged_event(CALL_2, accept=True, judged_at=at(3), rubric="v1")
    for event in (first, again, other):
        await deliver(writer, TOPIC_JUDGED, event, at(20))
    assert len(store.items) == 2
    (row,) = store.rates
    assert (row.n, row.passed, row.size_band) == (2, 1, "all")
    assert row.run_id.startswith("live-")
    assert row.run_state is EnumEvalRunState.LIVE
    assert row.scorer_versions == ("v1+judge-1", "v2+judge-1")
