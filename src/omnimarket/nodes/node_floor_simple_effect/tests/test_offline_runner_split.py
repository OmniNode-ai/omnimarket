# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22605 AC2: an offline runner is read against the host table, by the name the producer saves for it.

An offline runner whose host the table marks out or in repair is parked on purpose and reads as one line for that host.
An offline runner on a host that is available stays red and is named. A runner the saved file does not name cannot be
told apart, so it stays red as one unnamed line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import ModelFloorLabLine, ModelLabStateOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_runner_groups,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    iso,
    machine,
    overlay_text,
    record,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

H201_RUNNER = "omninode-runner-7"
H202_RUNNER = "spare-3"


def runner(name: str | None, status: str, host: str = "host-a") -> dict[str, Any]:
    row: dict[str, Any] = {"host": host, "status": status}
    if name is not None:
        row.update({"runner_name": name, "label_class": "omnibase-ci", "labels": []})
    return row


def fleet_doc(*runners: dict[str, Any]) -> dict[str, Any]:
    return {"status": "read", "observed_at": iso(NOW), "runners": list(runners)}


def floor_record(h201: str, h202: str) -> dict[str, Any]:
    rec = record()
    rec["floor"]["machines"] = [machine("h201", h201), machine("h202", h202)]
    return rec


def line_of(home: Path) -> ModelFloorLabLine:
    return next(x for x in written(home).lab_state if x.id == "runner_groups")


def test_golden_an_offline_runner_on_an_unavailable_host_reads_as_one_line_for_that_host(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path,
        rec=floor_record(h201="out", h202="read"),
        fleet_doc=fleet_doc(
            runner(H201_RUNNER, "offline"),
            runner("omninode-runner-8", "offline"),
            runner("omninode-runner-9", "online"),
        ),
    )
    run(home)
    assert line_of(home).state == "WORKING"
    overlay = ModelLabStateOverlay.model_validate(
        overlay_text("node_floor_simple_effect")
    )
    read = read_runner_groups(
        home / ".onex_state/runner-fleet/runner-fleet.json",
        overlay,
        floor_record(h201="out", h202="read"),
    )
    parked = [s for s in read.subjects if s.name == "h201 runners"]
    assert len(parked) == 1
    assert parked[0].ok
    assert parked[0].value == 2.0
    assert "2 runners offline, parked on purpose" in parked[0].note
    assert not any(s.name == H201_RUNNER for s in read.subjects)


def test_golden_an_offline_runner_on_an_available_host_stays_red_and_named(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path,
        rec=floor_record(h201="out", h202="read"),
        fleet_doc=fleet_doc(runner(H202_RUNNER, "offline", host="host-b")),
    )
    run(home)
    line = line_of(home)
    assert line.state == "NOT WORKING"
    assert H202_RUNNER in line.text
    assert "offline, meant to run" in line.text


def test_golden_the_same_runner_is_parked_or_red_by_whether_its_host_is_available(
    tmp_path: Path,
) -> None:
    doc = fleet_doc(runner(H201_RUNNER, "offline"))
    available = build_home(
        tmp_path / "available",
        rec=floor_record(h201="read", h202="read"),
        fleet_doc=doc,
    )
    unavailable = build_home(
        tmp_path / "unavailable",
        rec=floor_record(h201="repair", h202="read"),
        fleet_doc=doc,
    )
    run(available)
    run(unavailable)
    assert line_of(available).state == "NOT WORKING"
    assert H201_RUNNER in line_of(available).text
    assert line_of(unavailable).state == "WORKING"
    assert H201_RUNNER not in line_of(unavailable).text


def test_error_an_offline_runner_no_host_or_plan_row_names_stays_red_and_named(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path,
        rec=floor_record(h201="out", h202="out"),
        fleet_doc=fleet_doc(runner("stray-1", "offline", host="host-999")),
    )
    run(home)
    line = line_of(home)
    assert line.state == "NOT WORKING"
    assert "stray-1" in line.text
    assert "no host table or plan row names it" in line.text


def test_error_an_older_file_without_runner_names_reads_unsplit_and_stays_red(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path,
        rec=floor_record(h201="out", h202="out"),
        fleet_doc=fleet_doc(runner(None, "offline"), runner(None, "offline")),
    )
    run(home)
    line = line_of(home)
    assert line.state == "NOT WORKING"
    assert "unnamed runners" in line.text
    assert "2 offline runners carry no name" in line.text
