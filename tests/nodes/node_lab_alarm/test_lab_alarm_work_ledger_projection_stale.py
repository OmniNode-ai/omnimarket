# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger-projection-stale condition: the projection is not behind the ledger file by more than the bound (OMN-24400)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryGrowthStreaks,
    InMemoryLedgerNewest,
    InMemoryProjectionNewest,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import FileLedgerNewest
from omnimarket.nodes.node_lab_alarm.handlers.handler_work_ledger_projection_stale import (
    CONDITION,
    HandlerWorkLedgerProjectionStale,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_work_ledger_projection_stale import (
    EnumWorkLedgerProjectionStaleFault,
    EnumWorkLedgerProjectionStaleOutcome,
    ModelWorkLedgerProjectionStaleCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelConditionPersistence,
    ModelLabAlarmOverlay,
    ModelWorkLedgerProjectionStaleSettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import LedgerUnreadableError
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_work_ledger_projection_stale_handler,
    run_work_ledger_projection_stale,
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
_FILE_NEWEST = datetime(2026, 11, 12, 3, 59, tzinfo=UTC)
_NAME = "example.work_ledger_rows"
_SUBJECT = f"work-ledger-projection:{_NAME}"
_BOUND = 1800


def _settings(persist_runs: int = 1) -> ModelWorkLedgerProjectionStaleSettings:
    return ModelWorkLedgerProjectionStaleSettings(
        ledger_path=".onex_state/example-ledger.md",
        tail_bytes=262144,
        row_types=("CLAIM", "STATUS", "TERMINAL"),
        projection_name=_NAME,
        max_lag_seconds=_BOUND,
        projection_window_seconds=86400,
        timeout_seconds=30,
        persistence=ModelConditionPersistence(
            persist_runs=persist_runs,
            streak_path=".onex_state/lab-alarm/work-ledger-projection-streaks.json",
        ),
    )


def _command(
    *,
    write: bool = True,
    fault: EnumWorkLedgerProjectionStaleFault = EnumWorkLedgerProjectionStaleFault.NONE,
) -> ModelWorkLedgerProjectionStaleCommand:
    return ModelWorkLedgerProjectionStaleCommand(
        correlation_id="projection-1", write=write, fault=fault
    )


class _Rig:
    def __init__(
        self,
        projection_lag_seconds: float | None = 60,
        *,
        persist_runs: int = 1,
        deliver: bool = True,
    ) -> None:
        self.ledger = InMemoryLedgerNewest(newest=(_FILE_NEWEST, "CLAIM"))
        self.projection = InMemoryProjectionNewest(
            newest=None
            if projection_lag_seconds is None
            else _FILE_NEWEST - timedelta(seconds=projection_lag_seconds)
        )
        self.streaks = InMemoryGrowthStreaks()
        self.channel = InMemoryAlarmChannel()
        self.alarm_ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.alarm_ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerWorkLedgerProjectionStale(
            settings=_settings(persist_runs),
            ledger=self.ledger,
            projection=self.projection,
            streaks=self.streaks,
            delivery=delivery if deliver else None,
            clock=lambda: _CLOCK,
        )


def test_work_ledger_projection_stale_golden_chain() -> None:
    """A projection inside its bound reads OK with the chain started then completed; a breach alarms,
    delivers once through the delivery double, and the next OK ends the episode."""
    topics = load_topics()
    rig = _Rig(projection_lag_seconds=60)
    golden = rig.handler.handle(_command())
    assert golden.outcome is EnumWorkLedgerProjectionStaleOutcome.OK
    assert golden.alarms == ()
    assert golden.failure == ""
    assert "not a liveness statement" in golden.evidence
    assert [t for t, _ in events_for(golden, topics)] == [
        topics["work_ledger_projection_stale_run_started"],
        topics["work_ledger_projection_stale_run_completed"],
    ]
    rig.projection.newest_row = _FILE_NEWEST - timedelta(seconds=_BOUND + 1)
    breach = rig.handler.handle(_command())
    assert breach.outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM
    [alarm] = breach.alarms
    assert alarm.startswith(f"{_SUBJECT}: ")
    assert f"{_NAME} is {_BOUND + 1}s behind the ledger file" in alarm
    assert breach.delivery == (
        f"{CONDITION}:{_SUBJECT} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _NAME in text
    rig.projection.newest_row = _FILE_NEWEST
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumWorkLedgerProjectionStaleOutcome.OK
    assert rig.streaks.streaks == {}, "the OK run cleared the streak"


def test_work_ledger_projection_stale_error_chain() -> None:
    """An unreadable ledger or projection is INDETERMINATE, never ALARM or OK; a failed delivery names
    delivery-failed; the failed terminal event is published on a breach."""
    topics = load_topics()
    rig = _Rig()
    rig.ledger.error = "no such file"
    no_ledger = rig.handler.handle(_command())
    assert no_ledger.outcome is EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE
    assert no_ledger.reasons == ("ledger-unreadable",)
    assert no_ledger.alarms == ()
    assert no_ledger.failure == "indeterminate:ledger-unreadable"
    assert [t for t, _ in events_for(no_ledger, topics)][-1] == topics[
        "work_ledger_projection_stale_run_failed"
    ]
    assert rig.projection.since_calls == [], (
        "an unreadable ledger asks the projection nothing"
    )
    rig2 = _Rig()
    rig2.projection.error = "the query node gave no answer"
    no_projection = rig2.handler.handle(_command())
    assert no_projection.outcome is EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE
    assert no_projection.reasons == ("projection-unreadable",)
    assert no_projection.alarms == ()
    rig3 = _Rig(projection_lag_seconds=_BOUND + 600)
    rig3.alarm_ledger.refuse = "ledger host unreachable"
    failed = rig3.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    assert [t for t, _ in events_for(failed, topics)][-1] == topics[
        "work_ledger_projection_stale_run_failed"
    ]


def test_work_ledger_projection_stale_failing_control() -> None:
    """The bound in the overlay is what alarms: a lag of exactly the bound reads OK and one second over
    alarms; an empty projection window alarms; persist_runs 3 gives OK on runs 1 and 2 and ALARM on run 3,
    and a good read in between resets the streak."""
    at_bound = _Rig(projection_lag_seconds=_BOUND).handler.handle(_command())
    assert at_bound.outcome is EnumWorkLedgerProjectionStaleOutcome.OK
    over = _Rig(projection_lag_seconds=_BOUND + 1).handler.handle(_command())
    assert over.outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM
    empty = _Rig(projection_lag_seconds=None).handler.handle(_command())
    assert empty.outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM
    assert "holds no row since" in empty.alarms[0]
    rig = _Rig(projection_lag_seconds=_BOUND + 1, persist_runs=3)
    first = rig.handler.handle(_command())
    assert first.outcome is EnumWorkLedgerProjectionStaleOutcome.OK
    assert first.reasons == (f"waiting:{_SUBJECT}: run 1 of 3",)
    second = rig.handler.handle(_command())
    assert second.reasons == (f"waiting:{_SUBJECT}: run 2 of 3",)
    rig.projection.newest_row = _FILE_NEWEST
    reset = rig.handler.handle(_command())
    assert reset.reasons == ()
    assert rig.streaks.streaks == {}
    rig.projection.newest_row = _FILE_NEWEST - timedelta(seconds=_BOUND + 1)
    rig.handler.handle(_command())
    rig.handler.handle(_command())
    third = rig.handler.handle(_command())
    assert third.outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM
    assert third.reasons == ()


def test_work_ledger_projection_stale_seam() -> None:
    """The alarm line splits into a valid ModelLabAlarm the real delivery accepts, and every topic the
    handler emits is declared in the contract's topics and externally_consumed_topics."""
    rig = _Rig(projection_lag_seconds=_BOUND + 1)
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == _SUBJECT
    assert ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail).detail
    assert rig.channel.posts, "the real delivery handler took the alarm"
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    emitted = {t for t, _ in events_for(result, topics)}
    assert emitted <= set(contract["topics"].values())
    assert emitted <= set(contract["externally_consumed_topics"])


def test_work_ledger_projection_stale_asks_the_projection_from_the_window_start() -> (
    None
):
    rig = _Rig()
    rig.handler.handle(_command())
    assert rig.projection.since_calls == [_FILE_NEWEST - timedelta(seconds=86400)]


def test_work_ledger_projection_stale_a_probe_keeps_no_streak_and_a_write_does() -> (
    None
):
    rig = _Rig(projection_lag_seconds=_BOUND + 1, persist_runs=3)
    rig.handler.handle(_command(write=False))
    assert rig.streaks.streaks == {}
    assert rig.channel.posts == []
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {_SUBJECT: 1}
    rig.ledger.error = "no such file"
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {_SUBJECT: 1}, "an unreadable run leaves the streak"


def test_work_ledger_projection_stale_a_drill_alarms_without_reading_or_delivering() -> (
    None
):
    rig = _Rig()
    result = rig.handler.handle(
        _command(fault=EnumWorkLedgerProjectionStaleFault.PROJECTION_BEHIND)
    )
    assert result.outcome is EnumWorkLedgerProjectionStaleOutcome.ALARM
    assert rig.projection.since_calls == []
    assert rig.channel.posts == []
    assert rig.streaks.streaks == {}
    assert result.delivery == ()


def _example_raw() -> dict[str, object]:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_work_ledger_projection_stale_overlay_loads_without_the_block() -> None:
    raw = _example_raw()
    assert ModelLabAlarmOverlay.model_validate(raw).work_ledger_projection_stale
    del raw["work_ledger_projection_stale"]
    assert ModelLabAlarmOverlay.model_validate(raw).work_ledger_projection_stale is None


def test_work_ledger_projection_stale_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _example_raw()
    del raw["work_ledger_projection_stale"]
    code = run_work_ledger_projection_stale(
        "work-ledger-projection-stale",
        EnumWorkLedgerProjectionStaleFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no work_ledger_projection_stale block" in capsys.readouterr().err


def test_work_ledger_projection_stale_cli_exit_codes_follow_the_outcome() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    rig = _Rig(projection_lag_seconds=_BOUND + 1)
    lines: list[str] = []

    def run(verb: str) -> int:
        return run_work_ledger_projection_stale(
            verb,
            EnumWorkLedgerProjectionStaleFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )

    assert run("work-ledger-projection-stale-probe") == 2
    assert any(
        line.startswith("WORK-LEDGER-PROJECTION-STALE outcome=ALARM") for line in lines
    )
    assert any(line.startswith(f"ALARM {_SUBJECT}") for line in lines)
    rig.projection.newest_row = _FILE_NEWEST
    assert run("work-ledger-projection-stale") == 0
    rig.projection.error = "no answer"
    assert run("work-ledger-projection-stale") == 3


def test_work_ledger_projection_stale_cli_exits_alarm_when_the_delivery_fails() -> None:
    rig = _Rig(projection_lag_seconds=_BOUND + 1)
    rig.alarm_ledger.refuse = "ledger host unreachable"
    code = run_work_ledger_projection_stale(
        "work-ledger-projection-stale",
        EnumWorkLedgerProjectionStaleFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(_example_raw()),
        handler=rig.handler,
        out=lambda _: None,
    )
    assert code == 2


def test_work_ledger_projection_stale_inmemory_backend_builds_unread_fixtures() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    assert overlay.work_ledger_projection_stale is not None
    handler = build_work_ledger_projection_stale_handler(
        overlay, overlay.work_ledger_projection_stale, lambda: _CLOCK
    )
    result = handler.handle(_command(write=False))
    assert result.outcome is EnumWorkLedgerProjectionStaleOutcome.INDETERMINATE


def _write_ledger(path: Path, rows: list[str], pad: int = 0) -> None:
    path.write_text("x" * pad + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_file_ledger_newest_returns_the_newest_row_of_an_allowed_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.md"
    _write_ledger(
        path,
        [
            "2026-11-12T03:00:00Z | CLAIM | lane=a | text",
            "2026-11-12T03:30:00Z | STATUS | lane=a | text",
            "2026-11-12T03:45:00Z | RULING | lane=a | a type not allowed here",
        ],
    )
    stamp, row_type = FileLedgerNewest(path, 262144, ("CLAIM", "STATUS")).newest()
    assert (stamp, row_type) == (datetime(2026, 11, 12, 3, 30, tzinfo=UTC), "STATUS")


def test_file_ledger_newest_refuses_a_missing_file_and_a_file_without_a_row(
    tmp_path: Path,
) -> None:
    with pytest.raises(LedgerUnreadableError):
        FileLedgerNewest(tmp_path / "absent.md", 1024, ("CLAIM",)).newest()
    path = tmp_path / "empty.md"
    _write_ledger(path, ["prose, no canonical row"])
    with pytest.raises(LedgerUnreadableError):
        FileLedgerNewest(path, 1024, ("CLAIM",)).newest()


def test_file_ledger_newest_reads_only_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "ledger.md"
    _write_ledger(
        path,
        ["2026-11-12T03:00:00Z | CLAIM | lane=a | early row"]
        + ["2026-11-12T01:00:00Z | STATUS | lane=a | " + "y" * 200] * 3,
    )
    stamp, _ = FileLedgerNewest(path, 400, ("CLAIM", "STATUS")).newest()
    assert stamp == datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
    stamp, _ = FileLedgerNewest(path, 262144, ("CLAIM", "STATUS")).newest()
    assert stamp == datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
