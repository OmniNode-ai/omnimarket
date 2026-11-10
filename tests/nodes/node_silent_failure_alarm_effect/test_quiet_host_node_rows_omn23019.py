# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reading node's ledger rows are not lanes, so they cannot make a host read quiet (OMN-23019).

On 2026-11-09 the alarm raised a quiet-host row for h053 at 19:10:06Z ("last hook event
18:25:43Z (44m ago); lanes ran on it for 35m"). h053's root filesystem was full from about 18:10Z
(the lab fill refused it at 18:41Z with 0.3G free and at 19:01Z with 0.0G free), so no lane ran on it
between its last lane's closing row at 18:26:28Z and the claims of 19:13:43Z. The only rows naming h053
in between were two reading nodes' STATUS rows, written at 18:35:06Z by node_runner_group_exposure_effect
and at 18:35:19Z by node_seam_deadline_probe_effect: each names the host it probed in its ``host=``
cell and ran on the host that runs the readings, not on h053. The first of them is exactly 35m before
the alarm, which is the "lanes ran on it for 35m" of the row. The control in each reading is the same
ledger with a lane's own rows in place of the node's: the host must read quiet there, which shows the
reading depends on who wrote the row.
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

NODE_ROW = (
    "{stamp} | STATUS | lane=runner-group-exposure | src=runner-group-exposure | "
    "actor=node:node_runner_group_exposure_effect | model=none | host={host} | probe=exposure | "
    "verdict=WORKING | a reading of the host"
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


def test_alarm_lane_stamps_reading_node_rows_do_not_make_a_host_quiet(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, NODE_ROW)
    raised = _tick(options, HOST_NOW + timedelta(minutes=50))
    assert "h101" not in raised, (
        "reading-node rows only: no lane ran on h101 after its last hook event"
    )


def test_alarm_lane_stamps_node_control_lane_rows_after_the_last_event_still_raise(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, LANE_ROW)
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))


def test_alarm_lane_stamps_a_lane_among_reading_node_rows_still_raises(
    tmp_path: Path,
) -> None:
    options = _after_last_event(tmp_path, NODE_ROW)
    with _ledger(options).open("a") as handle:
        for minutes in (20, 46):
            handle.write(
                LANE_ROW.format(
                    stamp=_stamp(HOST_NOW + timedelta(minutes=minutes)), host="h101"
                )
                + "\n"
            )
    assert "h101" in _tick(options, HOST_NOW + timedelta(minutes=50))
