# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each source of the work-state projection is read under its own budget and timeout (OMN-23014 AC1).

The budget, the per-attempt timeout, the attempt count and the oldest snapshot still worth serving are the overlay's
`source_budgets`, one row per source. A source over its budget serves its last snapshot marked stale with its age; the
other sources and the projection still read. The failing control stalls one source with a reader that blocks on an event
nobody sets until the test ends: without the budget layer the read never returns.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkItemState,
    EnumWorkStateSource,
    ModelPullRequestSource,
    ModelSourceBudget,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_budget import (
    FileSnapshotStore,
    InMemorySnapshotStore,
    SourceRead,
    SourceSpec,
    SourceUnreadableError,
    read_source_budgeted,
)
from omnimarket.nodes.node_work_state_projection_effect import runtime_work_state

pytestmark = pytest.mark.unit

SOURCES = tuple(EnumWorkStateSource)


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """The stalled reader blocks on this; set at teardown so no thread outlives the test."""
    event = threading.Event()
    yield event
    event.set()


def budget(
    *, timeout: float = 0.2, total: float = 0.3, attempts: int = 1, max_age: float = 600
) -> ModelSourceBudget:
    return ModelSourceBudget(
        timeout_seconds=timeout,
        budget_seconds=total,
        attempts=attempts,
        max_stale_age_seconds=max_age,
    )


def pr_spec(
    reader: Callable[[], SourceRead], source: EnumWorkStateSource
) -> SourceSpec:
    return SourceSpec(source=source, read=reader, item_type=ModelPullRequestSource)


def prs(*numbers: int) -> SourceRead:
    return SourceRead(
        items=tuple(
            ModelPullRequestSource(repo="r", number=n, state="OPEN") for n in numbers
        )
    )


def stall(release: threading.Event) -> Callable[[], SourceRead]:
    def read() -> SourceRead:
        release.wait()
        return prs(99)

    return read


# --- the budget layer on its own ----------------------------------------------------------------------------------


def test_control_the_stalled_reader_really_blocks(
    release: threading.Event,
) -> None:
    """The failing control's premise: called directly, the stall does not return. Only the budget layer bounds it."""
    done = threading.Event()
    threading.Thread(target=lambda: (stall(release)(), done.set()), daemon=True).start()
    assert done.wait(0.3) is False


def test_a_source_that_answers_in_budget_is_fresh_and_saved(now) -> None:
    store = InMemorySnapshotStore()
    read, status = read_source_budgeted(
        pr_spec(lambda: prs(1, 2), EnumWorkStateSource.PR_WATCHER),
        budget(),
        store,
        now,
    )
    assert [p.number for p in read.items] == [1, 2]
    assert status.reading is EnumSourceReading.FRESH
    assert status.age_seconds == 0
    saved = store.load(EnumWorkStateSource.PR_WATCHER)
    assert saved is not None
    assert saved.read_at == now
    assert len(saved.items) == 2


def test_a_stalled_source_serves_its_last_snapshot_stale_with_its_age(
    now, release: threading.Event
) -> None:
    store = InMemorySnapshotStore()
    earlier = now - timedelta(seconds=90)
    read_source_budgeted(
        pr_spec(lambda: prs(1), EnumWorkStateSource.PR_WATCHER),
        budget(),
        store,
        earlier,
    )
    started = time.monotonic()
    read, status = read_source_budgeted(
        pr_spec(stall(release), EnumWorkStateSource.PR_WATCHER),
        budget(timeout=0.1, total=0.15),
        store,
        now,
    )
    waited = time.monotonic() - started
    assert [p.number for p in read.items] == [1]
    assert status.reading is EnumSourceReading.STALE
    assert status.age_seconds == 90
    assert status.read_at == earlier
    assert "timed out" in status.cause
    assert waited < 1.0


def test_a_stalled_source_with_no_snapshot_is_unread_and_named(
    now, release: threading.Event
) -> None:
    read, status = read_source_budgeted(
        pr_spec(stall(release), EnumWorkStateSource.PR_WATCHER),
        budget(timeout=0.1, total=0.15),
        InMemorySnapshotStore(),
        now,
    )
    assert read.items == ()
    assert status.reading is EnumSourceReading.UNREAD
    assert "no snapshot" in status.cause


def test_a_snapshot_older_than_the_declared_maximum_is_not_served(
    now, release: threading.Event
) -> None:
    store = InMemorySnapshotStore()
    read_source_budgeted(
        pr_spec(lambda: prs(1), EnumWorkStateSource.PR_WATCHER),
        budget(),
        store,
        now - timedelta(seconds=700),
    )
    read, status = read_source_budgeted(
        pr_spec(stall(release), EnumWorkStateSource.PR_WATCHER),
        budget(timeout=0.1, total=0.15, max_age=600),
        store,
        now,
    )
    assert read.items == ()
    assert status.reading is EnumSourceReading.UNREAD
    assert "older than" in status.cause


def test_a_reader_that_raises_serves_the_snapshot_stale(now) -> None:
    store = InMemorySnapshotStore()
    read_source_budgeted(
        pr_spec(lambda: prs(5), EnumWorkStateSource.PR_WATCHER),
        budget(),
        store,
        now - timedelta(seconds=30),
    )

    def broken() -> SourceRead:
        raise SourceUnreadableError("state file absent")

    read, status = read_source_budgeted(
        pr_spec(broken, EnumWorkStateSource.PR_WATCHER), budget(), store, now
    )
    assert [p.number for p in read.items] == [5]
    assert status.reading is EnumSourceReading.STALE
    assert status.age_seconds == 30
    assert status.cause == "state file absent"


def test_the_budget_is_per_source_not_shared(now, release: threading.Event) -> None:
    """A slow source with a long declared budget finishes; a slow one with a short budget does not."""

    def slow() -> SourceRead:
        time.sleep(0.25)
        return prs(7)

    store = InMemorySnapshotStore()
    _, long_status = read_source_budgeted(
        pr_spec(slow, EnumWorkStateSource.PR_WATCHER),
        budget(timeout=1.0, total=1.0),
        store,
        now,
    )
    _, short_status = read_source_budgeted(
        pr_spec(slow, EnumWorkStateSource.LEDGER),
        budget(timeout=0.05, total=0.05),
        store,
        now,
    )
    assert long_status.reading is EnumSourceReading.FRESH
    assert short_status.reading is EnumSourceReading.UNREAD


def test_a_later_attempt_inside_the_budget_can_answer(now) -> None:
    calls: list[int] = []
    gate = threading.Event()

    def flaky() -> SourceRead:
        calls.append(1)
        if len(calls) == 1:
            gate.wait(5)
        return prs(3)

    try:
        read, status = read_source_budgeted(
            pr_spec(flaky, EnumWorkStateSource.PR_WATCHER),
            budget(timeout=0.1, total=1.0, attempts=2),
            InMemorySnapshotStore(),
            now,
        )
    finally:
        gate.set()
    assert [p.number for p in read.items] == [3]
    assert status.reading is EnumSourceReading.FRESH
    assert len(calls) == 2


def test_the_file_store_round_trips_across_processes(tmp_path: Path, now) -> None:
    first = FileSnapshotStore(tmp_path / "snaps")
    read_source_budgeted(
        pr_spec(lambda: prs(8), EnumWorkStateSource.PR_WATCHER),
        budget(),
        first,
        now - timedelta(seconds=10),
    )
    second = FileSnapshotStore(tmp_path / "snaps")
    saved = second.load(EnumWorkStateSource.PR_WATCHER)
    assert saved is not None
    assert saved.read_at == now - timedelta(seconds=10)


def test_a_corrupt_snapshot_file_reads_as_no_snapshot(tmp_path: Path) -> None:
    store = FileSnapshotStore(tmp_path)
    (tmp_path / "pr-watcher.json").write_text("{not json", encoding="utf-8")
    assert store.load(EnumWorkStateSource.PR_WATCHER) is None


# --- the overlay declares them ------------------------------------------------------------------------------------


def _rules_doc(rules: ModelWorkStateRules) -> dict[str, Any]:
    return json.loads(rules.model_dump_json())


def test_the_example_overlay_declares_a_budget_for_every_source(rules) -> None:
    assert set(rules.source_budgets) == set(SOURCES)
    for row in rules.source_budgets.values():
        assert row.timeout_seconds > 0
        assert row.budget_seconds > 0
        assert row.attempts >= 1
        assert row.max_stale_age_seconds > 0


def test_an_overlay_missing_a_source_budget_is_refused(rules) -> None:
    doc = _rules_doc(rules)
    del doc["source_budgets"]["ledger"]
    with pytest.raises(ValidationError, match="ledger"):
        ModelWorkStateRules.model_validate(doc)


def test_an_overlay_with_a_zero_budget_is_refused(rules) -> None:
    doc = _rules_doc(rules)
    doc["source_budgets"]["plans"]["budget_seconds"] = 0
    with pytest.raises(ValidationError):
        ModelWorkStateRules.model_validate(doc)


# --- the effect: one stalled source, the rest still read ---------------------------------------------------------


WATCHER = {
    "prs": {
        "omnimarket#9": {
            "facts": {
                "repo": "omnimarket",
                "number": 9,
                "title": "a pull request",
                "state": "OPEN",
                "head_sha": "abc",
            },
            "ci": {"red": []},
            "cls": "green",
        }
    }
}
PLAN = "# Plan\n\n### T1. First\n- **Ticket:** ZEP-50\n"
LINEAR = {
    "issues": [
        {"id": "ZEP-1", "title": "t", "statusType": "backlog", "project": "Zephyr"}
    ]
}


class FakeLedger:
    def __init__(self, release: threading.Event | None = None) -> None:
        self.release = release

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if self.release is not None:
            self.release.wait()
        return ModelLedgerRead(rows=())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".claude_scratch/pr-watcher").mkdir(parents=True)
    (tmp_path / ".claude_scratch/pr-watcher/state.json").write_text(
        json.dumps(WATCHER), encoding="utf-8"
    )
    (tmp_path / "beta/plans").mkdir(parents=True)
    (tmp_path / "beta/plans/p.md").write_text(PLAN, encoding="utf-8")
    (tmp_path / ".onex_state/linear-snapshot").mkdir(parents=True)
    (tmp_path / ".onex_state/linear-snapshot/linear-1.json").write_text(
        json.dumps(LINEAR), encoding="utf-8"
    )
    (tmp_path / "remote-lanes/x").mkdir(parents=True)
    (tmp_path / "remote-lanes/x/r.json").write_text(
        json.dumps({"lane": "x", "status": "exited", "exit": 0}), encoding="utf-8"
    )
    (tmp_path / ".onex_state/readings").mkdir(parents=True)
    (tmp_path / ".onex_state/readings/readings.jsonl").write_text(
        json.dumps({"key": "M0", "value": "PASS"}) + "\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def env(home: Path) -> dict[str, str]:
    return {
        "OMNI_HOME": str(home),
        "ONEX_WORK_STATE_PLANS_ROOT": str(home),
        "ONEX_REMOTE_LANE_DIR": str(home / "remote-lanes"),
    }


def short_budgets(
    rules: ModelWorkStateRules, **per_source: float
) -> ModelWorkStateRules:
    """The example overlay with every budget cut to a test-sized wait; `per_source` sets one source's budget."""
    doc = _rules_doc(rules)
    for name, row in doc["source_budgets"].items():
        row["timeout_seconds"] = per_source.get(name, 1.0)
        row["budget_seconds"] = per_source.get(name, 1.0)
        row["attempts"] = 1
    return ModelWorkStateRules.model_validate(doc)


def status_of(request, source: EnumWorkStateSource):
    [row] = [s for s in request.source_status if s.source is source]
    return row


def test_every_source_fresh_reads_every_source(rules, now, home, env) -> None:
    request = runtime_work_state.build_request(
        now,
        home,
        env,
        FakeLedger(),
        short_budgets(rules),
        store=InMemorySnapshotStore(),
    )
    assert {s.source for s in request.source_status} == set(SOURCES)
    assert {s.reading for s in request.source_status} == {EnumSourceReading.FRESH}
    assert [u for u in request.unread if not u.startswith("scope:")] == []
    assert [p.number for p in request.pull_requests] == [9]
    assert [t.task_id for t in request.plan_tasks] == ["T1"]
    assert [t.ticket_id for t in request.tickets] == ["ZEP-1"]
    assert [r.lane for r in request.receipts] == ["x"]
    assert [r.key for r in request.readings] == ["M0"]


def test_one_stalled_source_is_stale_and_the_others_still_read(
    rules, now, home, env, release: threading.Event
) -> None:
    store = InMemorySnapshotStore()
    # first tick: everything answers and every snapshot is saved
    runtime_work_state.build_request(
        now - timedelta(seconds=120),
        home,
        env,
        FakeLedger(),
        short_budgets(rules),
        store=store,
    )
    # second tick: the PR watcher read stalls
    started = time.monotonic()
    request = runtime_work_state.build_request(
        now,
        home,
        env,
        FakeLedger(),
        short_budgets(rules, **{"pr-watcher": 0.1}),
        store=store,
        reader_overrides={EnumWorkStateSource.PR_WATCHER: stall(release)},
    )
    waited = time.monotonic() - started
    watcher = status_of(request, EnumWorkStateSource.PR_WATCHER)
    assert watcher.reading is EnumSourceReading.STALE
    assert watcher.age_seconds == 120
    # the stale snapshot is the projection's pull requests, not the stalled reader's rows
    assert [p.number for p in request.pull_requests] == [9]
    for other in SOURCES:
        if other is not EnumWorkStateSource.PR_WATCHER:
            assert status_of(request, other).reading is EnumSourceReading.FRESH
    assert [t.ticket_id for t in request.tickets] == ["ZEP-1"]
    assert [r.key for r in request.readings] == ["M0"]
    assert [u for u in request.unread if not u.startswith("scope:")] == []
    # it waited its own budget, not every source's
    assert waited < 0.9


def test_a_stalled_source_does_not_hold_back_the_others(
    rules, now, home, env, release: threading.Event
) -> None:
    """The stalled ledger takes its whole budget; the run does not take the sum of the budgets."""
    started = time.monotonic()
    request = runtime_work_state.build_request(
        now,
        home,
        env,
        FakeLedger(release),
        short_budgets(rules, ledger=0.4),
        store=InMemorySnapshotStore(),
    )
    waited = time.monotonic() - started
    assert status_of(request, EnumWorkStateSource.LEDGER).reading is (
        EnumSourceReading.UNREAD
    )
    assert any(u.startswith("ledger:") for u in request.unread)
    assert [p.number for p in request.pull_requests] == [9]
    assert waited < 0.9


def test_the_projection_still_projects_with_a_stale_source(
    rules, now, home, env, release: threading.Event
) -> None:
    store = InMemorySnapshotStore()
    runtime_work_state.build_request(
        now - timedelta(seconds=60),
        home,
        env,
        FakeLedger(),
        short_budgets(rules),
        store=store,
    )
    request = runtime_work_state.build_request(
        now,
        home,
        env,
        FakeLedger(),
        short_budgets(rules, linear=0.1),
        store=store,
        reader_overrides={EnumWorkStateSource.LINEAR: stall(release)},
    )
    result = HandlerWorkStateProjection().handle(request)
    assert {s.source: s.reading for s in result.source_status}[
        EnumWorkStateSource.LINEAR
    ] is EnumSourceReading.STALE
    states = {i.key: i.state for i in result.items}
    assert states["ticket:ZEP-1"] is EnumWorkItemState.AVAILABLE


def test_the_deployed_snapshot_store_is_the_contracts_runtime_directory() -> None:
    contract = yaml.safe_load(
        Path(runtime_work_state.__file__)
        .with_name("contract.yaml")
        .read_text(encoding="utf-8")
    )
    snaps = contract["runtime"]["snapshots"]
    assert snaps["dir_env"]
    assert snaps["relpath"]
