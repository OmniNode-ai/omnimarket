# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step on its real backend (OMN-23409).

The declared sources are the landing controller's facts file (overwritten every tick) and the PR
watcher's state (node ids, and failed run ids at the CI head). The recorder is an append-only
journal. One pass of the runtime records the tick the facts file holds now and writes its chain
to the floor's emission journal.

Golden: two consecutive controller ticks, each taken by one pass, are both in the journal after
the second; each PR's node id and its head's failed run ids come from the watcher state.
Error: a watcher state past its bound ends the tick in write_facts_unread with a failure chain
and no record; an unreadable facts file exits 3 and records nothing.
Failing control: a recorder that overwrites its one file every tick, as the controller does with
its facts, loses the first tick after the second.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_errors import (
    SnapshotSourceError,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    ControllerFactsFileSource,
    JsonlLandingTickRecorder,
    WatcherStateWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
    ModelLandingTickRecord,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import run_once
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    SECOND,
    overlay,
    scenario_facts,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
OWNER = "acme"


def _unfilled(tick: int) -> ModelLandingFacts:
    """The controller's facts for a tick: what its pinned decision reads, with no write facts."""
    facts = scenario_facts(tick)
    return facts.model_copy(
        update={
            "prs": tuple(
                p.model_copy(update={"pr_node_id": None, "failed_run_ids": ()})
                for p in facts.prs
            )
        }
    )


def _watcher_state(
    ticks: tuple[int, ...], *, at: datetime = NOW, moved: str | None = None
) -> dict[str, object]:
    prs: dict[str, object] = {}
    for tick in ticks:
        for i, pr in enumerate(scenario_facts(tick).prs):
            short = pr.pr.partition("/")[2]
            number = short.partition("#")[2]
            head = "f" * 40 if pr.pr == moved else pr.head_sha
            red = (
                [
                    [
                        "Tests (Split 1/2)",
                        "failure",
                        f"{number}0",
                        "2026-11-10T02:00:00Z",
                    ],
                    [
                        "Tests (Split 2/2)",
                        "failure",
                        f"{number}0",
                        "2026-11-10T02:00:01Z",
                    ],
                    ["lint", "failure", f"{number}1", "2026-11-10T02:00:02Z"],
                ]
                if i
                else []
            )
            prs[short] = {
                "facts": {
                    "head_sha": head,
                    "node_id": f"PR_kwDOn{number}",
                    "repo": short.partition("#")[0],
                },
                "ci": {"sha": head, "red_facts": red},
            }
    return {"last_tick": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "prs": prs}


def _write(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8"
    )


def _real_overlay(tmp: Path) -> ModelSnapshotOverlay:
    return ModelSnapshotOverlay.model_validate(
        {
            **overlay().model_dump(),
            "backend": "redpanda",
            "bootstrap_servers": "broker.invalid:9092",
            "facts_path": str(tmp / "last-facts.json"),
            "watcher_state_path": str(tmp / "state.json"),
            "watcher_max_age_seconds": 900,
            "owner": OWNER,
            "record_journal": str(tmp / "records.jsonl"),
            "emission_journal": str(tmp / "emissions.jsonl"),
        }
    )


@pytest.fixture
def sink() -> InMemoryLandingSnapshotPublisher:
    """The publisher the pass is given; the broker leg is the join test's."""
    return InMemoryLandingSnapshotPublisher()


def _pass(
    ov: ModelSnapshotOverlay, sink: InMemoryLandingSnapshotPublisher
) -> tuple[int, dict[str, object]]:
    return run_once(ov, now=lambda: NOW, publisher=sink)


def _emissions(tmp: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in (tmp / "emissions.jsonl").read_text().splitlines()
    ]


def test_watcher_source_reads_node_ids_and_the_heads_failed_run_ids_once_each(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "state.json", _watcher_state((FIRST,)))
    source = WatcherStateWriteFactsSource(
        tmp_path / "state.json", owner=OWNER, max_age_seconds=900, now=lambda: NOW
    )
    prs = scenario_facts(FIRST).prs
    read = source.read_write_facts({p.pr: p.head_sha for p in prs})
    first, second = (read[p.pr] for p in prs)
    n1, n2 = (int(p.pr.partition("#")[2]) for p in prs)
    assert (first.pr_node_id, first.failed_run_ids) == (f"PR_kwDOn{n1}", ())
    # two red checks of one run and one of another: each run once, in the order first read
    assert (second.pr_node_id, second.failed_run_ids) == (
        f"PR_kwDOn{n2}",
        (n2 * 10, n2 * 10 + 1),
    )


def test_golden_two_controller_ticks_each_taken_by_one_pass_are_both_recorded(
    tmp_path: Path, sink: InMemoryLandingSnapshotPublisher
) -> None:
    _write(tmp_path / "state.json", _watcher_state((FIRST, SECOND)))
    ov = _real_overlay(tmp_path)
    _write(tmp_path / "last-facts.json", _unfilled(FIRST).model_dump_json())
    code1, line1 = _pass(ov, sink)
    again, same = _pass(ov, sink)
    _write(
        tmp_path / "last-facts.json", _unfilled(SECOND).model_dump_json()
    )  # the controller's next tick overwrites it
    code2, line2 = _pass(ov, sink)
    assert (code1, line1["status"], line1["tick"]) == (0, "recorded", FIRST)
    assert (again, same) == (0, {"status": "already_recorded", "tick": FIRST})
    assert (code2, line2["status"], line2["tick"]) == (0, "recorded", SECOND)
    records = JsonlLandingTickRecorder(tmp_path / "records.jsonl").recorded()
    assert [r.tick for r in records] == [FIRST, SECOND]
    # the first tick's facts are recoverable after the second, with what the watcher filled
    first = records[0].facts
    assert first.model_copy(
        update={
            "prs": tuple(
                p.model_copy(update={"pr_node_id": None, "failed_run_ids": ()})
                for p in first.prs
            )
        }
    ) == _unfilled(FIRST)
    for i, pr in enumerate(first.prs):
        number = int(pr.pr.partition("#")[2])
        assert pr.pr_node_id == f"PR_kwDOn{number}"
        assert pr.failed_run_ids == ((number * 10, number * 10 + 1) if i else ())
    assert [t for t, _ in sink.published] == [ov.record_topic, ov.record_topic]
    phases = [(e["phase"], e["node"]) for e in _emissions(tmp_path)]
    assert (
        phases
        == [
            ("start", "node_pr_landing_snapshot_effect"),
            ("success", "node_pr_landing_snapshot_effect"),
        ]
        * 2
    )


def test_a_moved_head_fills_nothing_for_that_pr(tmp_path: Path) -> None:
    moved = scenario_facts(FIRST).prs[1].pr
    _write(tmp_path / "state.json", _watcher_state((FIRST,), moved=moved))
    source = WatcherStateWriteFactsSource(
        tmp_path / "state.json", owner=OWNER, max_age_seconds=900, now=lambda: NOW
    )
    heads = {p.pr: p.head_sha for p in scenario_facts(FIRST).prs}
    read = source.read_write_facts(heads)
    assert moved not in read
    assert set(read) == set(heads) - {moved}


def test_a_pr_under_another_owner_is_not_read(tmp_path: Path) -> None:
    _write(tmp_path / "state.json", _watcher_state((FIRST,)))
    source = WatcherStateWriteFactsSource(
        tmp_path / "state.json", owner="other", max_age_seconds=900, now=lambda: NOW
    )
    assert (
        source.read_write_facts({p.pr: p.head_sha for p in scenario_facts(FIRST).prs})
        == {}
    )


def test_error_a_watcher_state_past_its_bound_ends_in_write_facts_unread_and_records_nothing(
    tmp_path: Path, sink: InMemoryLandingSnapshotPublisher
) -> None:
    _write(
        tmp_path / "state.json",
        _watcher_state((FIRST,), at=NOW - timedelta(seconds=901)),
    )
    _write(tmp_path / "last-facts.json", _unfilled(FIRST).model_dump_json())
    code, line = _pass(_real_overlay(tmp_path), sink)
    assert (code, line["status"], line["failure_reason"]) == (
        1,
        "failed",
        "write_facts_unread",
    )
    assert "past its bound of 900s" in str(line["detail"])
    assert JsonlLandingTickRecorder(tmp_path / "records.jsonl").recorded() == ()
    [(topic, event)] = sink.published
    assert topic == overlay().failed_topic
    assert isinstance(event, ModelLandingSnapshotFailedEvent)
    assert event.reason is EnumSnapshotFailureReason.WRITE_FACTS_UNREAD
    [start, failure] = _emissions(tmp_path)
    assert (start["phase"], failure["phase"], failure["failure_code"]) == (
        "start",
        "failure",
        "write_facts_unread",
    )


def test_error_an_unreadable_facts_file_exits_three_and_records_nothing(
    tmp_path: Path, sink: InMemoryLandingSnapshotPublisher
) -> None:
    _write(tmp_path / "state.json", _watcher_state((FIRST,)))
    _write(tmp_path / "last-facts.json", "{not json")
    code, line = _pass(_real_overlay(tmp_path), sink)
    assert (code, line["status"]) == (3, "facts_unread")
    assert sink.published == []
    assert not (tmp_path / "records.jsonl").exists()


def test_error_a_facts_file_holding_another_tick_is_a_tick_mismatch(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "last-facts.json", _unfilled(SECOND).model_dump_json())
    source = ControllerFactsFileSource(tmp_path / "last-facts.json")
    assert source.read_facts(FIRST) is None
    assert source.current_tick() == SECOND
    _write(tmp_path / "last-facts.json", "[]")
    with pytest.raises(SnapshotSourceError, match="not landing facts"):
        source.read_facts(SECOND)


def test_the_redpanda_overlay_names_every_field_it_needs() -> None:
    base = overlay().model_dump()
    with pytest.raises(
        ValueError,
        match="facts_path, watcher_state_path, watcher_max_age_seconds, owner, record_journal, emission_journal",
    ):
        ModelSnapshotOverlay.model_validate(
            {**base, "backend": "redpanda", "bootstrap_servers": "b:1"}
        )


class _OverwritingRecorder:
    """The controller's way: one file, overwritten every tick."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, record: ModelLandingTickRecord) -> None:
        self._path.write_text(record.model_dump_json() + "\n", encoding="utf-8")

    def recorded(self) -> tuple[ModelLandingTickRecord, ...]:
        return JsonlLandingTickRecorder(self._path).recorded()


def _two_ticks(
    tmp: Path, recorder: ProtocolLandingTickRecorder
) -> tuple[ModelLandingTickRecord, ...]:
    _write(tmp / "state.json", _watcher_state((FIRST, SECOND)))
    for tick in (FIRST, SECOND):
        _write(tmp / "last-facts.json", _unfilled(tick).model_dump_json())
        handler = HandlerPrLandingSnapshot(
            overlay=overlay(),
            facts_source=ControllerFactsFileSource(tmp / "last-facts.json"),
            write_source=WatcherStateWriteFactsSource(
                tmp / "state.json", owner=OWNER, max_age_seconds=900, now=lambda: NOW
            ),
            recorder=recorder,
            publisher=InMemoryLandingSnapshotPublisher(),
        )
        result = handler.handle(
            ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=tick)
        )
        assert result.status is EnumSnapshotStatus.RECORDED
    return recorder.recorded()


def test_the_journal_keeps_the_first_tick_after_the_second_across_restarts(
    tmp_path: Path,
) -> None:
    _two_ticks(tmp_path, JsonlLandingTickRecorder(tmp_path / "records.jsonl"))
    reopened = JsonlLandingTickRecorder(tmp_path / "records.jsonl").recorded()
    assert [r.tick for r in reopened] == [FIRST, SECOND]


def test_failing_control_a_recorder_that_overwrites_its_file_loses_the_first_tick(
    tmp_path: Path,
) -> None:
    kept = _two_ticks(tmp_path, _OverwritingRecorder(tmp_path / "records.jsonl"))
    assert [r.tick for r in kept] == [SECOND]
