# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor draws the broker lag condition from the lag node's finished runs (piece 17's reading).

The alarm script's record watches a fixed list of consumer groups. One of them, the delegation writer's own
group, has had no member since the writer was folded into the runtime, and its lag grows with every
delegation event, so that record read ALARM at every tick and held piece 17 at NOT WORKING (measured on the
dev broker: state Empty, 0 members, lag 2550 then 2554 then 2555 in 80 s). The node derives the live groups,
so its run replaces that one condition. Golden: the stale ALARM in the script's record no longer shows. Error:
a lag node that alarms, that is missing, that cannot be parsed or that is old each draw NOT WORKING.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes.node_floor_simple_effect.support import (
    build_home,
    lab_alarm_run,
    lag_node_run,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

_LAG_RECORD = ".onex_state/lab-alarm/broker-lag-runs.jsonl"


def _broker_line(home: Path):
    run(home)
    return next(x for x in written(home).lab_state if x.id == "broker_lag")


def test_golden_the_scripts_stale_group_alarm_does_not_hold_the_lab_down(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path, alarm_run=lab_alarm_run(outcome="ALARM"), lag_run=lag_node_run("OK")
    )
    line = _broker_line(home)
    assert line.state == "WORKING"
    assert line.draw == "green"


def test_golden_piece_17_reads_working_when_every_lab_source_does(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path, alarm_run=lab_alarm_run(outcome="ALARM"), lag_run=lag_node_run("OK")
    )
    run(home)
    piece = next(p for p in written(home).pieces if p.piece == 17)
    assert "lab sources not working" not in piece.text


def test_error_a_lag_node_that_alarms_draws_not_working_and_says_which_condition(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, lag_run=lag_node_run("ALARM"))
    line = _broker_line(home)
    assert line.state == "NOT WORKING"
    assert "consumer_group_lag" in line.text or "ALARM" in line.text


def test_error_a_lag_node_that_could_not_read_is_not_working(tmp_path: Path) -> None:
    home = build_home(tmp_path, lag_run=lag_node_run("INDETERMINATE"))
    assert _broker_line(home).state == "NOT WORKING"


def test_error_a_missing_lag_node_record_is_unread_and_never_the_scripts_reading(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, alarm_run=lab_alarm_run(outcome="OK"))
    (home / _LAG_RECORD).unlink()
    line = _broker_line(home)
    assert line.state != "WORKING"
    assert "lag node's run record cannot be opened" in line.text


def test_error_an_unparseable_lag_node_record_is_unread(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    (home / _LAG_RECORD).write_text("not json\n", encoding="utf-8")
    line = _broker_line(home)
    assert line.state != "WORKING"
    assert "lag node's last run record cannot be parsed" in line.text


def test_error_a_record_without_the_lag_condition_is_unread(tmp_path: Path) -> None:
    home = build_home(
        tmp_path,
        lag_run={
            "finished_at": lag_node_run()["finished_at"],
            "reports": [{"condition": "other", "outcome": "OK", "evidence": ""}],
        },
    )
    assert _broker_line(home).state != "WORKING"


def test_error_an_old_lag_node_run_is_stale_even_when_the_script_ran_just_now(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, lag_run=lag_node_run("OK", finished_minutes_ago=40))
    line = _broker_line(home)
    assert line.state == "NOT WORKING"
    assert line.text.startswith("NOT WORKING: stale: read 40 min ago")
