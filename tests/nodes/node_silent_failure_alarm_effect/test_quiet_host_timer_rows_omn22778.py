# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host timer's own ledger rows are not lanes, so they cannot make a host read quiet (OMN-22778).

On 2026-11-07 the alarm raised quiet-host rows for h051 and h202 ("lanes ran on it for 38m" and
"58m", ledger 16:45:32Z and 16:51:02Z). Between each host's last hook event and those rows the only
rows naming the hosts were the host-drift timer's drift-pass STATUS rows (``actor=timer:``) and an
install report written from another host. A timer pass is not a lane: it runs no session, so it
sends no hook event, and counting it made a host with no lane at all read as a host that ran lanes
and sent nothing. The control in each reading is the same ledger with a lane's own rows in place of
the timer's: the host must read quiet there, which shows the reading depends on who wrote the row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    RECORDED,
    world,
)
from tests.nodes.node_silent_failure_alarm_effect.test_quiet_host_dedupe_omn22777 import (
    _events,
    _ledger,
    _stamp,
    _tick,
)

pytestmark = pytest.mark.unit

TIMER_ROW = (
    "{stamp} | STATUS | lane=host-drift-{host} | ticket=OMN-17427 | actor=timer:onex-host-drift | "
    "model=none | host={host} | action=drift-pass | exit=4 | host-drift pass"
)
LANE_ROW = (
    "{stamp} | CLAIM | lane=recorded | ticket=OMN-17427 | actor=script:onex-remote-lane | "
    "host={host} | a lane on the host"
)


def _after_last_event(
    tmp_path: Path, row: str, host: str = "h101"
) -> ModelSilentFailureAlarmOptions:
    """The recorded world with ``host`` healthy at HOST_NOW + 9m, then ``row`` rows at +20m and +45m."""
    options = world(tmp_path)
    _events(options, [f"{host}|{_stamp(HOST_NOW + timedelta(minutes=9))}"])
    with _ledger(options).open("a") as handle:
        for minutes in (20, 45):
            handle.write(
                row.format(
                    stamp=_stamp(HOST_NOW + timedelta(minutes=minutes)), host=host
                )
                + "\n"
            )
    return options


def test_alarm_lane_stamps_host_drift_timer_rows_do_not_make_a_host_quiet(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, TIMER_ROW)
    raised = _tick(options, HOST_NOW + timedelta(minutes=50))
    assert "h101" not in raised, (
        "timer rows only: no lane ran on h101 after its last hook event"
    )


def test_alarm_lane_stamps_control_lane_rows_after_the_last_event_still_raise(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, LANE_ROW)
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))


def test_alarm_lane_stamps_a_lane_among_timer_rows_still_raises(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, TIMER_ROW)
    with _ledger(options).open("a") as handle:
        for minutes in (20, 46):
            handle.write(
                LANE_ROW.format(
                    stamp=_stamp(HOST_NOW + timedelta(minutes=minutes)), host="h101"
                )
                + "\n"
            )
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))


def test_alarm_lane_stamps_the_recorded_hosts_still_read_quiet(tmp_path: Path) -> None:
    options = world(tmp_path)
    assert set(_tick(options, HOST_NOW)) == set(RECORDED)
