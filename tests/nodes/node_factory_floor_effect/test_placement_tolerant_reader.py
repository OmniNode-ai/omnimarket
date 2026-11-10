# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23435: the floor's placement reader takes a new optional field on a host capacity advertisement as a degraded
reading and never refuses the run. The failing control is the strict model the old reader validated each row with."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_factory_floor_effect.runtime_factory_floor import main
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_placement,
    read_placement,
)
from tests.nodes.node_factory_floor_effect.test_runtime_factory_floor import (
    _argv,
    _output,
)

_PLANTED = "planted_future_field"


def _row(host: str, **extra: Any) -> dict[str, Any]:
    return {
        "host_name": host,
        "cores": 8,
        "load1": 0.5,
        "mem_available_bytes": 8589934592,
        "running_units": 1,
        "max_units": 3,
        "advertised_at": "2026-10-31T11:59:55Z",
        "cadence_seconds": 10,
        **extra,
    }


def _hosts_file(tmp_path: Path, rows: Any) -> Path:
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_golden_every_declared_row_is_read_and_nothing_is_degraded(
    tmp_path: Path,
) -> None:
    reading = read_placement(_hosts_file(tmp_path, [_row("h202"), _row("h201")]))
    assert [host.host_name for host in reading.hosts] == ["h201", "h202"]
    assert reading.unknown_fields == ()
    assert reading.unreadable_rows == ()
    assert reading.cause == ""


def test_seam_the_writer_model_round_trips_through_the_reader_with_every_field(
    tmp_path: Path,
) -> None:
    """The seam with the lab-work advertiser: a field the writer's model declares is never reported unknown."""
    written = ModelHostCapacityAdvertisement.model_validate(
        _row(
            "h201",
            mem_total_bytes=17179869184,
            swap_in_kb_s=1.5,
            swap_out_kb_s=2.5,
            slot_cap_record="mem",
            capacity_refusal="swap",
            tools=["docker"],
            rank_penalty=0.25,
        )
    )
    reading = read_placement(_hosts_file(tmp_path, [written.model_dump(mode="json")]))
    assert reading.hosts == (written,)
    assert reading.unknown_fields == ()


def test_a_missing_file_is_an_empty_reading(tmp_path: Path) -> None:
    assert read_placement(tmp_path / "absent.json").hosts == ()


def test_a_planted_unknown_field_is_named_and_the_row_is_still_read(
    tmp_path: Path,
) -> None:
    path = _hosts_file(tmp_path, [_row("h201", **{_PLANTED: "x"}), _row("h202")])
    reading = read_placement(path)
    assert [host.host_name for host in reading.hosts] == ["h201", "h202"]
    assert reading.unknown_fields == (f"h201.{_PLANTED}",)
    assert _PLANTED in reading.cause
    assert [host.host_name for host in load_placement(path)] == ["h201", "h202"]


def test_a_row_that_fails_validation_is_skipped_and_named_and_the_rest_are_kept(
    tmp_path: Path,
) -> None:
    reading = read_placement(
        _hosts_file(tmp_path, [_row("h201", cores=0), _row("h202"), "not a row"])
    )
    assert [host.host_name for host in reading.hosts] == ["h202"]
    assert len(reading.unreadable_rows) == 2
    assert reading.unreadable_rows[0].startswith("h201: cores")
    assert reading.unreadable_rows[1].startswith("row 2")


def test_a_file_that_is_not_a_list_still_refuses(tmp_path: Path) -> None:
    with pytest.raises(SourceRefusedError):
        read_placement(_hosts_file(tmp_path, {"h201": _row("h201")}))


def test_failing_control_the_strict_model_refuses_the_same_planted_row() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelHostCapacityAdvertisement.model_validate(_row("h201", **{_PLANTED: "x"}))


def test_the_floor_run_with_a_planted_field_on_the_bus_publishes_and_names_it(
    world: dict[str, Path],
) -> None:
    hosts = world["home"] / ".claude_scratch" / "lab-run" / "hosts.json"
    hosts.write_text(json.dumps([_row("h201", **{_PLANTED: "x"})]), encoding="utf-8")
    assert main(_argv(world, "--plan", "beta/plans/2026-10-")) == 0
    document = json.loads(_output(world).read_text(encoding="utf-8"))
    placement = next(
        entry
        for entry in document["sources"]["health"]
        if entry["source"] == "placement"
    )
    assert placement["state"] == "read"
    assert f"h201.{_PLANTED}" in placement["cause"]
