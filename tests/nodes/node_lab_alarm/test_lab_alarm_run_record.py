# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm node leaves each finished run in the record the floor reads (piece 17, OMN-22864).

Golden: a run that finds no growth appends an OK line in the floor's run shape and the floor reads it
as the lag condition. Error: growth, an underivable manifest and an unwritable record each end in their
typed outcome, a drill never writes the record, and a record that cannot be written is a typed failure
that routes to the failed event and its own exit code, never a silent skip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import FileRunRecords
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.protocols import RunRecordUnwritableError
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import run
from tests.nodes.node_lab_alarm.test_lab_alarm_chain_events import (
    _flat,
    _manifest,
    _overlay,
)

pytestmark = pytest.mark.unit

_AT = datetime(2026, 11, 8, 1, 30, 5, tzinfo=UTC)


def _handler(
    lags: dict[str, int],
    previous: dict[str, int] | None,
    records: InMemoryRunRecords,
    manifest: InMemoryManifest | None = None,
) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=_overlay(),
        manifest=manifest or InMemoryManifest(_manifest()),
        lag=InMemoryLag(lags),
        samples=InMemorySamples(previous),
        records=records,
        clock=lambda: _AT,
    )


def _cmd(fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE, write: bool = True):
    return ModelBrokerLagCommand(correlation_id="rec-1", fault=fault, write=write)


def test_golden_a_flat_run_appends_one_ok_line_in_the_floors_run_shape() -> None:
    records = InMemoryRunRecords()
    result = _handler(_flat(7), _flat(7), records).handle(_cmd())

    assert result.outcome is EnumBrokerLagOutcome.OK
    assert result.failure == ""
    assert len(records.records) == 1
    line = records.records[0]
    assert line["finished_at"] == "2026-11-08T01:30:05Z"
    (report,) = line["reports"]
    assert report["condition"] == _overlay().lag.record_condition
    assert report["outcome"] == "OK"


def test_golden_the_file_record_appends_json_lines(tmp_path: Path) -> None:
    store = FileRunRecords(tmp_path / "deep" / "runs.jsonl")
    store.append({"finished_at": "2026-11-08T01:30:05Z", "reports": []})
    store.append({"finished_at": "2026-11-08T01:35:05Z", "reports": []})
    lines = (tmp_path / "deep" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["finished_at"] for x in lines] == [
        "2026-11-08T01:30:05Z",
        "2026-11-08T01:35:05Z",
    ]


def test_error_growth_is_recorded_as_alarm_naming_the_group() -> None:
    records = InMemoryRunRecords()
    grown = dict.fromkeys(_flat(), 50)
    result = _handler(grown, _flat(7), records).handle(_cmd())

    assert result.outcome is EnumBrokerLagOutcome.ALARM
    (report,) = records.records[0]["reports"]
    assert report["outcome"] == "ALARM"
    assert "lag grew 7->50" in report["evidence"]


def test_error_an_orphan_group_that_grows_is_not_watched_so_it_cannot_alarm() -> None:
    """The measured defect: a group nobody consumes keeps growing. Only derived groups are read."""
    records = InMemoryRunRecords()
    lags = {**_flat(7), "local.legacy.delegation-writer.consume.v1": 9_999}
    previous = {**_flat(7), "local.legacy.delegation-writer.consume.v1": 1}
    result = _handler(lags, previous, records).handle(_cmd())

    assert result.outcome is EnumBrokerLagOutcome.OK
    assert records.records[0]["reports"][0]["outcome"] == "OK"


def test_error_an_underivable_manifest_is_recorded_as_indeterminate() -> None:
    records = InMemoryRunRecords()
    manifest = InMemoryManifest(_manifest())
    manifest.unreadable = "manifest fetch failed (URLError)"
    result = _handler(_flat(7), _flat(7), records, manifest).handle(_cmd())

    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    (report,) = records.records[0]["reports"]
    assert report["outcome"] == "INDETERMINATE"
    assert "derivation" in report["evidence"]


@pytest.mark.parametrize(
    "fault", [EnumLabAlarmFault.PLANTED_LAG, EnumLabAlarmFault.UNKNOWN_NODE]
)
def test_error_a_drill_never_writes_the_record_the_board_reads(
    fault: EnumLabAlarmFault,
) -> None:
    records = InMemoryRunRecords()
    _handler(_flat(7), _flat(7), records).handle(_cmd(fault))
    assert records.records == []


def test_error_a_probe_never_writes_the_record() -> None:
    records = InMemoryRunRecords()
    _handler(_flat(7), _flat(7), records).handle(_cmd(write=False))
    assert records.records == []


def test_error_an_unwritable_record_is_a_typed_failure_on_the_failed_event() -> None:
    records = InMemoryRunRecords()
    records.unwritable = "run record unwritable (PermissionError)"
    result = _handler(_flat(7), _flat(7), records).handle(_cmd())

    assert result.outcome is EnumBrokerLagOutcome.OK
    assert result.failure == "record-unwritable:run record unwritable (PermissionError)"
    events = events_for(result, load_topics())
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]


def test_error_the_file_record_names_the_type_of_a_failure_never_its_text(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    store = FileRunRecords(blocker / "runs.jsonl")
    with pytest.raises(RunRecordUnwritableError) as caught:
        store.append({"finished_at": "x", "reports": []})
    assert str(caught.value).startswith("run record unwritable (")
    assert str(blocker) not in str(caught.value)


def test_error_the_command_exits_4_when_the_record_cannot_be_written() -> None:
    records = InMemoryRunRecords()
    records.unwritable = "run record unwritable (OSError)"
    out: list[str] = []
    code = run(
        "broker-lag",
        EnumLabAlarmFault.NONE,
        overlay=_overlay(),
        handler=_handler(_flat(7), _flat(7), records),
        out=lambda text: out.append(text),
    )
    assert code == 4
