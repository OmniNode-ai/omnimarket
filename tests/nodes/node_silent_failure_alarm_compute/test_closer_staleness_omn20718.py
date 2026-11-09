# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket closer's staleness alarm (OMN-20718).

On 2026-10-27 the closer's launchd unit was removed and nothing alarmed for 11.5 days: the only
reading was a failed-run reading (exit 70 on every run that flipped nothing), and a unit that
never fires leaves no failed run. The alarm below reads the age of the closer's newest TERMINAL
or run record against its declared interval and goes red past twice that interval.

Each RED reading has a control with the one fact that makes it true removed. The named injected
break is the booted-out unit: the closer's newest record 11.5 days old and its unit absent from
the scheduler's table.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.silent_failure_alarm import (
    ModelCloserStalenessRequest,
    ModelDrainerObservation,
)
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerCloserStalenessAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelCloserLivenessOptions,
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    Watch,
    world,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
INTERVAL = 7200
UNIT = "ai.omninode.dod-closeout-sweep"
PROCESS = "h200/dod-closeout-sweep"


def _request(**fields: object) -> ModelCloserStalenessRequest:
    base: dict[str, object] = {
        "now": NOW,
        "lane": "dod-closeout-sweep",
        "unit_label": UNIT,
        "interval_seconds": INTERVAL,
    }
    return ModelCloserStalenessRequest.model_validate({**base, **fields})


def test_golden_chain_a_record_inside_twice_the_interval_is_live() -> None:
    result = HandlerCloserStalenessAlarm().handle(
        _request(last_record_at=NOW - timedelta(seconds=2 * INTERVAL), unit_loaded=True)
    )
    assert not result.alarm
    assert result.state == "LIVE"
    assert result.limit_seconds == 2 * INTERVAL
    assert result.friction_cells == ""
    assert result.alert_text == ""


def test_golden_chain_past_twice_the_interval_is_red_with_friction_cells() -> None:
    result = HandlerCloserStalenessAlarm().handle(
        _request(
            last_record_at=NOW - timedelta(seconds=2 * INTERVAL + 1), unit_loaded=True
        )
    )
    assert result.alarm
    assert result.state == "STALE"
    assert "class=closer-staleness" in result.friction_cells
    assert "limit_seconds=14400" in result.friction_cells
    assert "CLOSER STALE" in result.alert_text


def test_booted_out_unit_for_eleven_and_a_half_days_is_red_and_names_the_unit() -> None:
    result = HandlerCloserStalenessAlarm().handle(
        _request(last_record_at=NOW - timedelta(days=11, hours=12), unit_loaded=False)
    )
    assert result.alarm
    assert "11d12h old" in result.cause
    assert f"does not list {UNIT}" in result.cause
    # CONTROL: the same age with the unit listed still alarms (the age is the finding), but
    # does not claim the unit is gone.
    listed = HandlerCloserStalenessAlarm().handle(
        _request(last_record_at=NOW - timedelta(days=11, hours=12), unit_loaded=True)
    )
    assert listed.alarm
    assert "does not list" not in listed.cause


def test_no_record_at_all_is_red_not_a_pass() -> None:
    result = HandlerCloserStalenessAlarm().handle(_request(last_record_at=None))
    assert result.alarm
    assert "no run record" in result.cause


def test_error_chain_unreadable_or_undeclared_is_never_live() -> None:
    handler = HandlerCloserStalenessAlarm()
    assert (
        handler.handle(_request(interval_seconds=None, last_record_at=NOW)).state
        == "UNCONFIGURED"
    )
    unreadable = handler.handle(_request(records_readable=False))
    assert unreadable.state == "UNKNOWN"
    assert not unreadable.alarm


def test_error_chain_a_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelCloserStalenessRequest.model_validate(
            {"now": "not a time", "lane": "", "unit_label": UNIT, "stale_multiple": 0}
        )
    assert {err["loc"][0] for err in caught.value.errors()} >= {
        "now",
        "lane",
        "stale_multiple",
    }


# --- the tick, as the host scheduler runs it ---------------------------------------------------


def _overlay(path: Path, *, declared: bool = True) -> Path:
    entry = (
        f"  - process_id: {PROCESS}\n    expected_interval_seconds: {INTERVAL}\n"
        if declared
        else "  - process_id: h200/other\n    expected_interval_seconds: 60\n"
    )
    path.write_text(
        "schema_version: automation-liveness-overlay/v1\nprocesses:\n" + entry
    )
    return path


def _launchctl(path: Path, labels: list[str]) -> Path:
    lines = "".join(f'  echo "-\t0\t{label}"\n' for label in labels)
    path.write_text(
        f'#!/bin/sh\nif [ "$1" = list ]; then\n  echo "PID\tStatus\tLabel"\n{lines}  exit 0\nfi\nexit 1\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _terminal(at: datetime, flipped: int = 0) -> str:
    return (
        f"{at.strftime('%Y-%m-%dT%H:%M:%SZ')} | TERMINAL | lane=dod-closeout-sweep | "
        f"run=run-{at:%Y%m%d%H%M} | actor=launchd:dod-closeout-sweep | flipped={flipped} | "
        "friction=none | no report: nothing changed"
    )


def _tick(
    tmp_path: Path,
    *,
    terminal_age: timedelta | None,
    unit_listed: bool,
    closer: bool = True,
    declared: bool = True,
    receipts: list[dict[str, str]] | None = None,
):
    options = world(tmp_path)
    assert options.ledger is not None
    if terminal_age is not None:
        with options.ledger.open("a") as handle:
            handle.write(_terminal(HOST_NOW - terminal_age) + "\n")
    fire_receipts = None
    if receipts is not None:
        fire_receipts = tmp_path / "fire-receipts.jsonl"
        fire_receipts.write_text("".join(json.dumps(r) + "\n" for r in receipts))
    labels = ["ai.omninode.hourly-tick"] + ([UNIT] if unit_listed else [])
    options = options.model_copy(
        update={
            "launchctl": str(_launchctl(tmp_path / "launchctl.sh", labels)),
            "closer": ModelCloserLivenessOptions(
                liveness_overlay=_overlay(tmp_path / "overlay.yaml", declared=declared),
                fire_receipts=fire_receipts,
            )
            if closer
            else None,
        }
    )
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in options.host_aliases
    }
    return HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW)
    )


def _closer_rows(result) -> tuple[str, ...]:
    return tuple(r for r in result.rows if "class=closer-staleness" in r)


def test_tick_injected_booted_out_unit_goes_red_and_the_control_stays_green(
    tmp_path: Path,
) -> None:
    (tmp_path / "red").mkdir()
    red = _tick(
        tmp_path / "red", terminal_age=timedelta(days=11, hours=12), unit_listed=False
    )
    assert "closer-staleness" in red.alarms
    assert "closer_staleness=STALE" in red.summary
    (row,) = _closer_rows(red)
    assert f"does not list {UNIT}" in row
    # CONTROL: the unit listed and a TERMINAL 90 minutes old.
    (tmp_path / "green").mkdir()
    green = _tick(
        tmp_path / "green", terminal_age=timedelta(minutes=90), unit_listed=True
    )
    assert "closer-staleness" not in green.alarms
    assert "closer_staleness=LIVE" in green.summary
    assert _closer_rows(green) == ()


def test_tick_a_run_that_flipped_nothing_is_a_live_closer(tmp_path: Path) -> None:
    result = _tick(tmp_path, terminal_age=timedelta(minutes=30), unit_listed=True)
    assert "closer_staleness=LIVE" in result.summary


def test_tick_the_bound_is_twice_the_declared_interval(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    inside = _tick(
        tmp_path / "in", terminal_age=timedelta(seconds=14399), unit_listed=True
    )
    outside = _tick(
        tmp_path / "out", terminal_age=timedelta(seconds=14401), unit_listed=True
    )
    assert "closer_staleness=LIVE" in inside.summary
    assert "closer_staleness=STALE" in outside.summary


def test_tick_a_failed_fire_receipt_counts_as_the_closer_having_run(
    tmp_path: Path,
) -> None:
    stamp = (HOST_NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = _tick(
        tmp_path,
        terminal_age=timedelta(days=11),
        unit_listed=True,
        receipts=[{"ts": stamp, "workflow": "dod-closeout-sweep", "phase": "failed"}],
    )
    assert "closer_staleness=LIVE" in result.summary
    # CONTROL: the same receipt for another workflow does not keep this closer alive.
    (tmp_path / "other").mkdir()
    other = _tick(
        tmp_path / "other",
        terminal_age=timedelta(days=11),
        unit_listed=True,
        receipts=[{"ts": stamp, "workflow": "lab-fill", "phase": "completed"}],
    )
    assert "closer_staleness=STALE" in other.summary


def test_tick_error_chain_unconfigured_and_undeclared_are_not_a_pass(
    tmp_path: Path,
) -> None:
    (tmp_path / "none").mkdir()
    (tmp_path / "undeclared").mkdir()
    none = _tick(tmp_path / "none", terminal_age=None, unit_listed=False, closer=False)
    undeclared = _tick(
        tmp_path / "undeclared", terminal_age=None, unit_listed=False, declared=False
    )
    assert "closer_staleness=UNCONFIGURED" in none.summary
    assert "closer_staleness=UNCONFIGURED" in undeclared.summary
    assert "closer-staleness" not in none.alarms


def test_options_model_is_a_sibling_of_the_alarm_options() -> None:
    assert "closer" in ModelSilentFailureAlarmOptions.model_fields
