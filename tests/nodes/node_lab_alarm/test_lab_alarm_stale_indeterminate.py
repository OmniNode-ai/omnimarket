# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale-indeterminate condition of node_lab_alarm and its seam to the alarm delivery.

Golden: a watched condition whose last runs are all INDETERMINATE alarms, is delivered once through the
delivery double, and the next OK ends the episode; a condition that reads a verdict is not stale. Error:
an unreadable record store is INDETERMINATE, never ALARM and never OK; a failed delivery exits as an
alarm and names delivery-failed; an unwritable record names record-unwritable. Failing control: fewer
runs on record than the bound reads OK, and the bound in the overlay is what alarms; a drill alarms
without reading, keeping or delivering. Seam: the alarm line a run produces, split at the first ': ',
builds a valid ModelLabAlarm accepted by the real HandlerLabAlarmDeliver with the in-memory backend;
every topic name the handler's events_for emits is declared in the contract's topics and
externally_consumed_topics. The overlay loads without the block, the verb refuses an overlay without the
block, the CLI exit codes follow the outcome, and the FileRunRecordReader reads a local JSONL file,
refuses a non-JSON line, and the journal exit appends the exit code as an outcome.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    FileRunRecordReader,
    InMemoryRunRecordReader,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRunRecords,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_stale_indeterminate import (
    CONDITION,
    HandlerStaleIndeterminate,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_stale_indeterminate import (
    EnumStaleIndeterminateFault,
    EnumStaleIndeterminateOutcome,
    ModelStaleIndeterminateCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelLabAlarmOverlay,
    ModelStaleIndeterminateSettings,
    ModelWatchedCondition,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    RunRecordUnreadableError,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    journal_exit,
    run_stale_indeterminate,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
_COND = "example-condition"


def _settings(
    watched: tuple[ModelWatchedCondition, ...] = (
        ModelWatchedCondition(
            condition=_COND, record_path=".onex_state/lab-alarm/example-condition.jsonl"
        ),
    ),
    after_runs: int = 3,
) -> ModelStaleIndeterminateSettings:
    return ModelStaleIndeterminateSettings(
        watched=watched,
        after_runs=after_runs,
        tail_bytes=262144,
    )


def _command(
    *,
    write: bool = True,
    fault: EnumStaleIndeterminateFault = EnumStaleIndeterminateFault.NONE,
) -> ModelStaleIndeterminateCommand:
    return ModelStaleIndeterminateCommand(
        correlation_id="stale-indeterminate-1", write=write, fault=fault
    )


class _Rig:
    def __init__(
        self,
        outcomes: dict[str, list[str]] | None = None,
        *,
        settings: ModelStaleIndeterminateSettings | None = None,
        deliver: bool = True,
        records: InMemoryRunRecords | None = None,
    ) -> None:
        self.reader = InMemoryRunRecordReader(outcomes=outcomes or {})
        self.records = records if records is not None else InMemoryRunRecords()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerStaleIndeterminate(
            settings=settings if settings is not None else _settings(),
            reader=self.reader,
            delivery=delivery if deliver else None,
            records=self.records,
            clock=lambda: _CLOCK,
        )


def test_stale_indeterminate_golden_chain() -> None:
    """A watched condition whose last runs are all INDETERMINATE alarms, is delivered once through the
    delivery double and the next OK ends the episode; a condition that reads a verdict is not stale."""
    topics = load_topics()
    rig = _Rig(outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]})
    alarm = rig.handler.handle(_command())
    assert alarm.outcome is EnumStaleIndeterminateOutcome.ALARM
    [line] = alarm.alarms
    assert line.startswith(f"stale-indeterminate:{_COND}: ")
    assert "has read INDETERMINATE on its last 3 runs" in line
    assert "with no OK and no ALARM to clear it" in line
    assert alarm.readings == (f"{_COND}: stale",)
    assert alarm.delivery == (
        f"stale_indeterminate:stale-indeterminate:{_COND} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _COND in text
    [(_, row)] = rig.ledger.rows
    assert f"subject=stale-indeterminate:{_COND}" in row.split(" | ")
    assert [t for t, _ in events_for(alarm, topics)] == [
        topics["stale_indeterminate_run_started"],
        topics["stale_indeterminate_run_failed"],
    ]
    rig.reader.outcomes = {_COND: ["OK", "INDETERMINATE", "INDETERMINATE"]}
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumStaleIndeterminateOutcome.OK
    assert ok.readings == (f"{_COND}: reads a verdict",)
    assert ok.alarms == ()
    assert [t for t, _ in events_for(ok, topics)] == [
        topics["stale_indeterminate_run_started"],
        topics["stale_indeterminate_run_completed"],
    ]
    rig2 = _Rig(
        outcomes={
            _COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"],
            "other-condition": ["INDETERMINATE", "ALARM", "OK"],
        },
        settings=_settings(
            watched=(
                ModelWatchedCondition(
                    condition=_COND,
                    record_path=".onex_state/lab-alarm/example-condition.jsonl",
                ),
                ModelWatchedCondition(
                    condition="other-condition",
                    record_path=".onex_state/lab-alarm/other-condition.jsonl",
                ),
            )
        ),
    )
    mixed = rig2.handler.handle(_command())
    assert mixed.outcome is EnumStaleIndeterminateOutcome.ALARM
    assert mixed.readings == (f"{_COND}: stale", "other-condition: reads a verdict")
    rig3 = _Rig(outcomes={_COND: ["OK", "OK", "OK"]})
    fresh = rig3.handler.handle(_command())
    assert fresh.outcome is EnumStaleIndeterminateOutcome.OK
    assert fresh.readings == (f"{_COND}: reads a verdict",)


def test_stale_indeterminate_error_chain() -> None:
    """An unreadable record store is INDETERMINATE, never ALARM and never OK; a failed delivery names
    delivery-failed; an unwritable record names record-unwritable."""
    topics = load_topics()
    rig = _Rig()
    rig.reader.error = "record store offline"
    unreadable = rig.handler.handle(_command())
    assert unreadable.outcome is EnumStaleIndeterminateOutcome.INDETERMINATE
    assert unreadable.reasons == ("record-unreadable:record store offline",)
    assert unreadable.alarms == ()
    assert unreadable.failure == "indeterminate:record-unreadable:record store offline"
    assert [t for t, _ in events_for(unreadable, topics)][-1] == topics[
        "stale_indeterminate_run_failed"
    ]
    rig2 = _Rig(outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]})
    rig2.ledger.refuse = "ledger host unreachable"
    failed = rig2.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    assert [t for t, _ in events_for(failed, topics)][-1] == topics[
        "stale_indeterminate_run_failed"
    ]
    rig3 = _Rig(outcomes={_COND: ["OK", "OK", "OK"]})
    rig3.records.unwritable = "disk full"
    unwritable = rig3.handler.handle(_command())
    assert unwritable.outcome is EnumStaleIndeterminateOutcome.OK
    assert unwritable.failure == "record-unwritable:disk full"


def test_stale_indeterminate_failing_control() -> None:
    """Fewer runs on record than the bound reads OK, and the bound in the overlay is what alarms; a
    drill alarms without reading, keeping or delivering."""
    rig = _Rig(
        outcomes={_COND: ["INDETERMINATE", "INDETERMINATE"]},
        settings=_settings(after_runs=3),
    )
    short = rig.handler.handle(_command())
    assert short.outcome is EnumStaleIndeterminateOutcome.OK
    assert short.readings == (f"{_COND}: 2 of 3 runs on record",)
    rig2 = _Rig(
        outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]},
        settings=_settings(after_runs=3),
    )
    assert (
        rig2.handler.handle(_command()).outcome is EnumStaleIndeterminateOutcome.ALARM
    )
    rig3 = _Rig(
        outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]},
        settings=_settings(after_runs=4),
    )
    assert rig3.handler.handle(_command()).outcome is EnumStaleIndeterminateOutcome.OK
    rig4 = _Rig()
    rig4.reader.error = "record store offline"
    drill = rig4.handler.handle(
        _command(fault=EnumStaleIndeterminateFault.PLANTED_STALE)
    )
    assert drill.outcome is EnumStaleIndeterminateOutcome.ALARM
    [line] = drill.alarms
    assert "injected: planted-stale" in line
    assert rig4.records.records == []
    assert rig4.channel.posts == []
    assert drill.delivery == ()


def test_stale_indeterminate_seam() -> None:
    """The alarm line a run produces, split at the first ': ', builds a valid ModelLabAlarm accepted by
    the real HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's events_for
    emits is declared in the contract's topics and externally_consumed_topics."""
    rig = _Rig(outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]})
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == f"stale-indeterminate:{_COND}"
    model = ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
    assert model.condition == CONDITION
    assert model.subject == subject
    assert model.detail == detail
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["topics"])
    external = set(contract.get("externally_consumed_topics", []))
    emitted = {t for t, _ in events_for(result, topics)}
    assert {k for k, v in topics.items() if v in emitted} <= declared
    assert emitted <= set(contract["topics"].values())
    assert emitted <= external, "every emitted topic is declared as externally consumed"


def test_stale_indeterminate_probe_delivers_and_records_nothing() -> None:
    """A probe (write=False) reads but neither delivers nor records."""
    rig = _Rig(outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]})
    result = rig.handler.handle(_command(write=False))
    assert result.outcome is EnumStaleIndeterminateOutcome.ALARM
    assert result.delivery == ()
    assert rig.channel.posts == []
    assert rig.records.records == []


def test_stale_indeterminate_cli_exit_codes_follow_the_outcome() -> None:
    """The CLI exit codes follow the outcome: 0 for OK, 2 for ALARM, 3 for INDETERMINATE."""
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    rig = _Rig(outcomes={_COND: ["INDETERMINATE", "INDETERMINATE", "INDETERMINATE"]})
    lines: list[str] = []
    assert (
        run_stale_indeterminate(
            "stale-indeterminate-probe",
            EnumStaleIndeterminateFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 2
    )
    assert any(
        "outcome=ALARM" in line and line.startswith("STALE-INDETERMINATE")
        for line in lines
    )
    assert any(line.startswith(f"ALARM stale-indeterminate:{_COND}") for line in lines)
    rig2 = _Rig(outcomes={_COND: ["OK", "OK", "OK"]})
    assert (
        run_stale_indeterminate(
            "stale-indeterminate",
            EnumStaleIndeterminateFault.NONE,
            overlay=overlay,
            handler=rig2.handler,
            out=lines.append,
        )
        == 0
    )
    rig3 = _Rig()
    rig3.reader.error = "record store offline"
    assert (
        run_stale_indeterminate(
            "stale-indeterminate",
            EnumStaleIndeterminateFault.NONE,
            overlay=overlay,
            handler=rig3.handler,
            out=lines.append,
        )
        == 3
    )


def test_stale_indeterminate_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The verb refuses an overlay without the stale_indeterminate block."""
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["stale_indeterminate"]
    code = run_stale_indeterminate(
        "stale-indeterminate",
        EnumStaleIndeterminateFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no stale_indeterminate block" in capsys.readouterr().err


def test_stale_indeterminate_overlay_loads_without_the_block() -> None:
    """The overlay loads without the stale_indeterminate block."""
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).stale_indeterminate is not None
    del raw["stale_indeterminate"]
    assert ModelLabAlarmOverlay.model_validate(raw).stale_indeterminate is None


def test_stale_indeterminate_file_reader_returns_newest_first(tmp_path: Path) -> None:
    """The records are appended oldest first; the reader returns the newest outcomes first."""
    path = tmp_path / "records.jsonl"
    outcomes_oldest_first = ["OK", "INDETERMINATE", "OK", "ALARM", "INDETERMINATE"]
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "finished_at": f"2026-11-12T0{n}:00:00Z",
                    "reports": [
                        {"condition": _COND, "outcome": outcome, "evidence": f"e{n}"},
                        {"condition": "other", "outcome": "ALARM", "evidence": "x"},
                    ],
                }
            )
            for n, outcome in enumerate(outcomes_oldest_first)
        )
        + "\n",
        encoding="utf-8",
    )
    reader = FileRunRecordReader(paths={_COND: path}, tail_bytes=262144)
    assert reader.recent_outcomes(_COND, 3) == ("INDETERMINATE", "ALARM", "OK")
    assert len(reader.recent_outcomes(_COND, 99)) == 5


def test_stale_indeterminate_file_reader_missing_file_returns_empty(
    tmp_path: Path,
) -> None:
    """A missing file returns an empty tuple."""
    reader = FileRunRecordReader(
        paths={_COND: tmp_path / "missing.jsonl"}, tail_bytes=262144
    )
    assert reader.recent_outcomes(_COND, 3) == ()


def test_stale_indeterminate_file_reader_non_json_line_raises(tmp_path: Path) -> None:
    """A non-JSON line raises RunRecordUnreadableError."""
    path = tmp_path / "records.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    reader = FileRunRecordReader(paths={_COND: path}, tail_bytes=262144)
    with pytest.raises(RunRecordUnreadableError):
        reader.recent_outcomes(_COND, 3)


def test_stale_indeterminate_file_reader_undeclared_condition_raises() -> None:
    """A condition with no declared path raises RunRecordUnreadableError."""
    reader = FileRunRecordReader(paths={}, tail_bytes=262144)
    with pytest.raises(RunRecordUnreadableError):
        reader.recent_outcomes("undeclared-condition", 3)


def test_stale_indeterminate_file_reader_tail_window_drops_partial_line(
    tmp_path: Path,
) -> None:
    """A window that opens mid-line never parses the partial first line; a window over the whole file reads every line."""
    path = tmp_path / "records.jsonl"
    full_line = json.dumps(
        {
            "finished_at": "2026-11-12T04:00:00Z",
            "reports": [
                {"condition": _COND, "outcome": "INDETERMINATE", "evidence": "e"}
            ],
        }
    )
    path.write_text(full_line + "\n" + full_line + "\n", encoding="utf-8")
    small = FileRunRecordReader(paths={_COND: path}, tail_bytes=len(full_line) + 10)
    assert small.recent_outcomes(_COND, 3) == ("INDETERMINATE",)
    whole = FileRunRecordReader(paths={_COND: path}, tail_bytes=10 * len(full_line))
    assert whole.recent_outcomes(_COND, 3) == ("INDETERMINATE", "INDETERMINATE")


def _overlay_with_record(record_path: Path) -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["verdict_record"] = {"path": str(record_path)}
    return ModelLabAlarmOverlay.model_validate(raw)


def test_stale_indeterminate_journal_exit_appends_the_exit_as_an_outcome(
    tmp_path: Path,
) -> None:
    """The journal appends the exit of a journaled verb as an outcome; the stale reader reads it back newest first (the seam between the verbs' journal and this verb's reader)."""
    record_path = tmp_path / "verdicts.jsonl"
    overlay = _overlay_with_record(record_path)
    for code in (0, 2, 3):
        assert (
            journal_exit(
                "dev-tag-heartbeat", code, overlay=overlay, clock=lambda: _CLOCK
            )
            == code
        )
    # Nothing is appended for another exit code, a probe form or an undeclared verb.
    for verb, code in (
        ("dev-tag-heartbeat", 1),
        ("dev-tag-heartbeat-probe", 0),
        ("broker-lag", 0),
    ):
        assert journal_exit(verb, code, overlay=overlay, clock=lambda: _CLOCK) == code
    reader = FileRunRecordReader(
        paths={"dev_tag_heartbeat": record_path}, tail_bytes=262144
    )
    assert reader.recent_outcomes("dev_tag_heartbeat", 5) == (
        "INDETERMINATE",
        "ALARM",
        "OK",
    )
    # An overlay with no verdict_record journals nothing.
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["verdict_record"]
    bare = ModelLabAlarmOverlay.model_validate(raw)
    assert journal_exit("dev-tag-heartbeat", 0, overlay=bare) == 0
    assert len(reader.recent_outcomes("dev_tag_heartbeat", 9)) == 3, (
        "the run with no verdict_record wrote nothing"
    )


def test_stale_indeterminate_journal_exit_unwritable_returns_4(tmp_path: Path) -> None:
    """A journal that cannot be written ends the run with exit 4, never a silent skip."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    overlay = _overlay_with_record(blocker / "verdicts.jsonl")
    assert (
        journal_exit("dev-tag-heartbeat", 0, overlay=overlay, clock=lambda: _CLOCK) == 4
    )
