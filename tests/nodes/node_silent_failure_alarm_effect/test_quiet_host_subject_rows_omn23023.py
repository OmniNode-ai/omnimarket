# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane's row about another host is not a lane on that host (OMN-23023).

After the host-drift timer rows were dropped from the quiet-host read (OMN-22778), h051 still read
quiet at 2026-11-07T16:45:32Z on one row: a STATUS of lane h051-online-1510 with ``host=h051``,
written while the lane ran on h053 (its CLAIM names ``host=h053``) to report the branch it had
installed on h051. The ``host=`` cell of that row names the host it describes, not the host the lane
ran on, and a lane on h053 sends no hook event on behalf of h051. The control in each reading is the
same row shape with the lane's CLAIM naming the described host, or a second lane's CLAIM there: the
host must read quiet, which shows the reading depends on where the lane ran.
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
    world,
)
from tests.nodes.node_silent_failure_alarm_effect.test_quiet_host_dedupe_omn22777 import (
    _events,
    _ledger,
    _stamp,
    _tick,
)

pytestmark = pytest.mark.unit

CLAIM = (
    "{stamp} | CLAIM | lane={lane} | ticket=OMN-17427 | actor=script:onex-remote-lane | "
    "host={host} | remote lane on {host}"
)
STATUS = (
    "{stamp} | STATUS | lane={lane} | ticket=OMN-17427 | actor=claude:sonnet:remote-lane | "
    "installed={host}:abc123 | host={host} | via=bus:{ran_on} | installed the branch on {host}"
)
TERMINAL = (
    "{stamp} | TERMINAL | lane={lane} | ticket=OMN-17427 | actor=claude:sonnet:remote-lane | "
    "outcome=partial | host={host} | closed"
)


def _append(options: ModelSilentFailureAlarmOptions, *rows: str) -> None:
    with _ledger(options).open("a") as handle:
        for row in rows:
            handle.write(row + "\n")


def _at(minutes: int) -> str:
    return _stamp(HOST_NOW + timedelta(minutes=minutes))


def _after_last_event(tmp_path: Path) -> ModelSilentFailureAlarmOptions:
    """The recorded world with h101 healthy at HOST_NOW + 9m."""
    options = world(tmp_path)
    _events(options, [f"h101|{_stamp(HOST_NOW + timedelta(minutes=9))}"])
    return options


def test_alarm_lane_stamps_a_lanes_row_naming_another_host_does_not_make_it_quiet(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path)
    _append(
        options,
        CLAIM.format(stamp=_at(15), lane="installer", host="h053"),
        STATUS.format(stamp=_at(20), lane="installer", host="h101", ran_on="h053"),
        STATUS.format(stamp=_at(45), lane="installer", host="h101", ran_on="h053"),
        TERMINAL.format(stamp=_at(46), lane="installer", host="h053"),
    )
    assert "h101" not in _tick(options, HOST_NOW + timedelta(minutes=50)), (
        "the lane ran on h053: its rows describing h101 are not lane activity on h101"
    )


def test_alarm_lane_stamps_control_the_lane_that_ran_on_the_host_still_raises(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path)
    _append(
        options,
        CLAIM.format(stamp=_at(15), lane="installer", host="h101"),
        STATUS.format(stamp=_at(20), lane="installer", host="h101", ran_on="h101"),
        STATUS.format(stamp=_at(45), lane="installer", host="h101", ran_on="h101"),
    )
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))


def test_alarm_lane_stamps_another_lane_that_ran_on_the_host_still_raises(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path)
    _append(
        options,
        CLAIM.format(stamp=_at(15), lane="installer", host="h053"),
        STATUS.format(stamp=_at(20), lane="installer", host="h101", ran_on="h053"),
        CLAIM.format(stamp=_at(16), lane="resident", host="h101"),
    )
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))


def test_alarm_lane_stamps_a_lane_with_no_claim_host_is_still_counted(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path)
    _append(
        options,
        STATUS.format(stamp=_at(20), lane="unclaimed", host="h101", ran_on="h101"),
        STATUS.format(stamp=_at(45), lane="unclaimed", host="h101", ran_on="h101"),
    )
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))
