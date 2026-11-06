# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner groups row reads the fleet file the floor's own fleet source saves (OMN-22549).

That file holds each runner as a host and a status, with no class rollup. The row used to refuse it as unread, so it
drew UNKNOWN even when the file was fresh and whole. It now groups the rows by host and counts offline runners
against the overlay's limit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import ModelFloorLabLine
from tests.nodes.node_floor_simple_effect.support import NOW, build_home, iso
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit


def host_rows(offline: int = 0) -> dict[str, object]:
    """The saved shape: a host label and a status per runner, as the floor's fleet source writes it."""
    runners = [{"host": "pool-a", "status": "online"}] * 3
    runners += [{"host": "pool-a", "status": "busy"}] * 2
    runners += [{"host": "pool-a", "status": "offline"}] * offline
    runners += [{"host": "pool-b", "status": "busy"}]
    return {
        "status": "read",
        "observed_at": iso(NOW),
        "runners": runners,
    }


def line_of(home: Path) -> ModelFloorLabLine:
    return next(x for x in written(home).lab_state if x.id == "runner_groups")


def test_golden_host_rows_are_grouped_by_host_and_read_working(tmp_path: Path) -> None:
    home = build_home(tmp_path, fleet_doc=host_rows())
    run(home)
    line = line_of(home)
    assert line.state == "WORKING"
    assert line.reading_age_s is not None


def test_golden_an_offline_runner_in_a_group_is_named_with_its_counts(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, fleet_doc=host_rows(offline=2))
    run(home)
    line = line_of(home)
    assert line.state == "NOT WORKING"
    assert "pool-a" in line.text
    assert "5 online, 2 offline" in line.text


def test_error_a_document_with_neither_runners_nor_a_rollup_reads_unknown(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, fleet_doc={"observed_at": iso(NOW)})
    run(home)
    line = line_of(home)
    assert line.state == "UNKNOWN"
    assert "no observed_at, runners or class_rollup" in line.text


def test_error_an_unread_document_keeps_its_producer_cause(tmp_path: Path) -> None:
    home = build_home(
        tmp_path, fleet_doc={"status": "unread", "reason": "no such container"}
    )
    run(home)
    line = line_of(home)
    assert line.state == "UNKNOWN"
    assert "no such container" in line.text
    assert json.loads(json.dumps(line.text))
