# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the readings feeder's file and the projection's readings reader (OMN-23427).

node_work_state_readings_feeder_scheduled_effect folds the ledger's reading rows into the readings file and
node_work_state_projection_effect's READINGS source reads it back as the projection's readings: the join holds only
while the feeder's written field names (from the projection contract's ``runtime:`` block) are the ones the reader
looks up, and the reader's path is the one the feeder writes. A row whose value cell the writer renamed is not a
reading: the feeder writes nothing for it and the reader is left with no readings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.work_state_feeder.model_readings_feed_rules import (
    ModelReadingsFeedRules,
)
from omnimarket.models.work_state_projection import EnumWorkStateSource
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_budget import (
    SourceUnreadableError,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    _runtime,
    source_readers,
)
from omnimarket.nodes.node_work_state_readings_feeder_scheduled_effect.protocols.adapter_readings_feeder import (
    ReadingsFeeder,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0,
    rules,
)

pytestmark = pytest.mark.unit


def _feed_rules(**override: object) -> ModelReadingsFeedRules:
    base: dict[str, object] = {
        "row_type": "STATUS",
        "term": "src=reading",
        "key_cell": "key",
        "value_cell": "verdict",
        "published_cell": "published_at",
        "lookback_hours": 72.0,
    }
    return ModelReadingsFeedRules.model_validate(base | override)


def _row(stamp: str, key: str, verdict: str, *, value_cell: str = "verdict") -> str:
    """One STATUS reading row, in the shape node_reading_writer_effect appends."""
    return (
        f"{stamp} | STATUS | lane=l | src=reading | key={key} "
        f"| {value_cell}={verdict} | published_at={stamp} | text"
    )


def _readings_file(home: Path) -> Path:
    rt = _runtime()
    return Path(home / rt["readings"]["relpath"])


def _read_projection_readings(home: Path) -> tuple[object, ...]:
    """The projection's READINGS source over the home, as its reader returns it.

    A source with nothing to serve (no file) is no readings, the same empty answer the projection
    reads when the source is named unread.
    """
    rt = _runtime()
    env = {rt["home_env"]: str(home)}
    spec = next(
        s
        for s in source_readers(T0, home, env, None, rules())
        if s.source is EnumWorkStateSource.READINGS
    )
    try:
        return spec.read().items
    except SourceUnreadableError:
        return ()


def test_three_rows_three_keys_reach_the_reader(tmp_path: Path) -> None:
    home = tmp_path
    ledger = InMemoryLedger(
        [
            _row("2026-11-10T01:00:00Z", "piece1-a", "WORKING"),
            _row("2026-11-10T01:30:00Z", "piece2-b", "BLOCKED"),
            _row("2026-11-10T02:00:00Z", "piece3-c", "WORKING"),
        ]
    )
    outcome = ReadingsFeeder(
        _feed_rules(), ledger, environ={"OMNI_HOME": str(home)}
    ).feed(T0)
    assert outcome.status == "fed"
    assert outcome.item_count == 3
    assert outcome.source_as_of == datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
    readings = {
        reading.key: reading.value for reading in _read_projection_readings(home)
    }
    assert readings == {
        "piece1-a": "WORKING",
        "piece2-b": "BLOCKED",
        "piece3-c": "WORKING",
    }


def test_newest_published_row_of_a_key_wins(tmp_path: Path) -> None:
    home = tmp_path
    ledger = InMemoryLedger(
        [
            _row("2026-11-09T20:00:00Z", "piece1-a", "BLOCKED"),
            # The same key, published later: one line in the file, the newer fact.
            _row("2026-11-09T21:00:00Z", "piece1-a", "WORKING"),
        ]
    )
    outcome = ReadingsFeeder(
        _feed_rules(), ledger, environ={"OMNI_HOME": str(home)}
    ).feed(T0)
    assert outcome.status == "fed"
    assert outcome.item_count == 1
    assert outcome.source_as_of == datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
    readings = {
        reading.key: reading.value for reading in _read_projection_readings(home)
    }
    assert readings == {"piece1-a": "WORKING"}


def test_renamed_value_cell_is_no_reading(tmp_path: Path) -> None:
    home = tmp_path
    ledger = InMemoryLedger(
        [
            # The writer renames the value's cell: the row still selects, but carries no value.
            _row("2026-11-10T02:00:00Z", "piece1-a", "WORKING", value_cell="finding"),
        ]
    )
    outcome = ReadingsFeeder(
        _feed_rules(), ledger, environ={"OMNI_HOME": str(home)}
    ).feed(T0)
    assert outcome.status == "not_fed"
    assert outcome.item_count == 0
    assert not _readings_file(home).exists()
    assert _read_projection_readings(home) == ()


def test_zero_rows_leaves_the_old_file_alone(tmp_path: Path) -> None:
    home = tmp_path
    target = _readings_file(home)
    target.parent.mkdir(parents=True)
    target.write_text('{"key": "piece0", "value": "WORKING"}\n', encoding="utf-8")
    outcome = ReadingsFeeder(
        _feed_rules(), InMemoryLedger(), environ={"OMNI_HOME": str(home)}
    ).feed(T0)
    assert outcome.status == "not_fed"
    assert outcome.cause == "no reading rows in the window"
    assert outcome.item_count == 0
    assert (
        target.read_text(encoding="utf-8") == '{"key": "piece0", "value": "WORKING"}\n'
    )


def test_from_environ_without_a_ledger_path_fails_naming_it(tmp_path: Path) -> None:
    rules_path = tmp_path / "feed-rules.yaml"
    rules_path.write_text(_feed_rules().model_dump_json(indent=1), encoding="utf-8")
    env = {
        "OMNI_HOME": str(tmp_path),
        "ONEX_WORK_STATE_READINGS_FEED_RULES": str(rules_path),
    }
    feeder = ReadingsFeeder.from_environ(env)
    outcome = feeder.feed(T0)
    assert outcome.status == "failed"
    assert "ONEX_LEDGER_PATH" in outcome.cause
