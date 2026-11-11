# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record journal keeps the newest records the overlay declares and reads only those (OMN-23757).

On 2026-11-11 the cutover unit's journal reached 22.9 MB and then 16.6 MB: nothing ever removed a record,
and every pass parsed the whole file for one answer, "is this tick already recorded". The broker holds each
record (it is published before it is journaled), so the journal needs only the newest few.

Golden: a bounded journal never holds more than its declared count, keeps the newest, and a pass over a
legacy oversized file parses only the declared count and leaves the file at that count.
Error chain: a zero or negative count is refused by the overlay; an absent key keeps the unbounded
behaviour and names the key on the pass line; a corrupt row in the kept tail is refused, never skipped.
Failing control: at the merge base the overlay has no such key (extra=forbid refuses it) and the journal
grows with every record and every read parses every record, so each test here is red there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers import (
    snapshot_sources,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.journal_retention import (
    RETENTION_KEY,
    EnumJournalRetention,
    decide_journal_retention,
    retention_line,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    JsonlLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelLandingTickRecord,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import run_once
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    scenario_facts,
)
from tests.nodes.node_pr_landing_snapshot_effect.test_snapshot_real_backend import (
    NOW,
    _real_overlay,
    _unfilled,
    _watcher_state,
    _write,
)

pytestmark = pytest.mark.unit

BASE_TICK = 1_794_400_000


def _record(tick: int) -> ModelLandingTickRecord:
    return ModelLandingTickRecord(
        correlation_id=FIXED_ID,
        tick=tick,
        facts=scenario_facts(FIRST).model_copy(update={"tick": tick}),
        provenance="test",
    )


def _legacy_journal(path: Path, count: int) -> list[int]:
    ticks = [BASE_TICK + 120 * i for i in range(count)]
    path.write_text("".join(_record(t).model_dump_json() + "\n" for t in ticks))
    return ticks


def _ticks_in(path: Path) -> list[int]:
    return [json.loads(line)["tick"] for line in path.read_text().splitlines()]


def test_decision_unbounded_without_a_declared_value_names_the_key() -> None:
    decision = decide_journal_retention(None)
    assert decision.retention is EnumJournalRetention.UNSET
    assert (decision.read_limit, decision.trim, decision.missing_key) == (
        None,
        False,
        RETENTION_KEY,
    )
    assert retention_line(decision, trimmed=False) == {
        "journal_retention": "unset",
        "journal_retention_missing_key": "record_journal_max_records",
    }


@pytest.mark.parametrize(
    ("found", "trim"), [(0, False), (29, False), (30, False), (31, True)]
)
def test_decision_trims_only_past_the_declared_count(found: int, trim: bool) -> None:
    decision = decide_journal_retention(30, records_found=found)
    assert decision.retention is EnumJournalRetention.BOUNDED
    assert (decision.read_limit, decision.trim) == (30, trim)
    assert retention_line(decision, trimmed=trim) == {
        "journal_retention": "bounded",
        "journal_max_records": 30,
        "journal_trimmed": trim,
    }


def test_a_bounded_journal_never_holds_more_than_its_count_and_keeps_the_newest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    recorder = JsonlLandingTickRecorder(path, max_records=5)
    for i in range(40):
        recorder.record(_record(BASE_TICK + i))
        assert len(_ticks_in(path)) <= 5
    assert _ticks_in(path) == [BASE_TICK + i for i in range(35, 40)]
    assert [r.tick for r in recorder.recorded()] == [
        BASE_TICK + i for i in range(35, 40)
    ]
    assert not list(tmp_path.glob("*.trim"))


def test_failing_control_an_unset_journal_grows_with_every_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    recorder = JsonlLandingTickRecorder(path)
    for i in range(40):
        recorder.record(_record(BASE_TICK + i))
    assert len(_ticks_in(path)) == 40
    assert len(recorder.recorded()) == 40


def test_a_read_of_a_legacy_oversized_journal_parses_only_the_declared_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "records.jsonl"
    ticks = _legacy_journal(path, 229)
    parsed: list[str] = []
    real = ModelLandingTickRecord.model_validate_json

    def counting(data: str | bytes, **kwargs: object) -> ModelLandingTickRecord:
        parsed.append("row")
        return real(data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        snapshot_sources.ModelLandingTickRecord, "model_validate_json", counting
    )
    kept = JsonlLandingTickRecorder(path, max_records=30).recorded()
    assert [r.tick for r in kept] == ticks[-30:]
    assert len(parsed) == 30


def test_a_tail_read_spanning_many_chunks_returns_whole_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshot_sources, "_TAIL_CHUNK", 97)
    path = tmp_path / "records.jsonl"
    ticks = _legacy_journal(path, 12)
    kept = JsonlLandingTickRecorder(path, max_records=4).recorded()
    assert [r.tick for r in kept] == ticks[-4:]


def test_the_first_append_to_a_legacy_oversized_journal_trims_it_to_the_count(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    ticks = _legacy_journal(path, 229)
    recorder = JsonlLandingTickRecorder(path, max_records=30)
    new_tick = ticks[-1] + 120
    recorder.record(_record(new_tick))
    assert recorder.trimmed
    assert _ticks_in(path) == [*ticks[-29:], new_tick]


def test_a_corrupt_row_in_the_kept_tail_is_refused_never_skipped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _legacy_journal(path, 3)
    with path.open("a") as fh:
        fh.write("not json\n")
    with pytest.raises(ValidationError):
        JsonlLandingTickRecorder(path, max_records=3).recorded()


@pytest.mark.parametrize("bad", [0, -1])
def test_the_overlay_refuses_a_count_below_one(tmp_path: Path, bad: int) -> None:
    with pytest.raises(ValidationError, match="record_journal_max_records"):
        ModelSnapshotOverlay.model_validate(
            {**_real_overlay(tmp_path).model_dump(), "record_journal_max_records": bad}
        )


def _bounded_overlay(tmp: Path, count: int | None) -> ModelSnapshotOverlay:
    return ModelSnapshotOverlay.model_validate(
        {**_real_overlay(tmp).model_dump(), "record_journal_max_records": count}
    )


def test_golden_a_pass_over_a_legacy_journal_trims_it_and_still_dedupes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _legacy_journal(path, 229)
    _write(tmp_path / "state.json", _watcher_state((FIRST,)))
    _write(tmp_path / "last-facts.json", _unfilled(FIRST).model_dump_json())
    ov = _bounded_overlay(tmp_path, 30)
    sink = InMemoryLandingSnapshotPublisher()
    code, line = run_once(ov, now=lambda: NOW, publisher=sink)
    assert (code, line["status"], line["tick"]) == (0, "recorded", FIRST)
    assert (line["journal_retention"], line["journal_max_records"]) == ("bounded", 30)
    assert line["journal_trimmed"] is True
    assert len(_ticks_in(path)) == 30
    assert _ticks_in(path)[-1] == FIRST
    code2, again = run_once(ov, now=lambda: NOW, publisher=sink)
    assert (code2, again["status"], again["tick"]) == (0, "already_recorded", FIRST)
    assert again["journal_trimmed"] is False
    assert (
        len(sink.published) == 1
    )  # the seam: one record on the topic, as without retention


def test_a_pass_with_the_key_absent_keeps_the_unbounded_journal_and_names_the_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _legacy_journal(path, 50)
    _write(tmp_path / "state.json", _watcher_state((FIRST,)))
    _write(tmp_path / "last-facts.json", _unfilled(FIRST).model_dump_json())
    code, line = run_once(
        _bounded_overlay(tmp_path, None),
        now=lambda: datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC),
        publisher=InMemoryLandingSnapshotPublisher(),
    )
    assert (code, line["status"]) == (0, "recorded")
    assert line["journal_retention"] == "unset"
    assert line["journal_retention_missing_key"] == "record_journal_max_records"
    assert len(_ticks_in(path)) == 51
