# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's Host disk card never counts a host it cannot vouch for as above the bar (OMN-23224).

On 2026-11-08 the served floor read "7 of 7 hosts above 10 GB free" at 21:16:05Z while h053 read 3.7 GB free in the lab
fill at 20:59:46Z and 7.6 GB in the throughput tick at 21:07:34Z. Those two figures are the recorded readings replayed
here; the receipt of 21:16Z itself was not readable from the lab host this test was written on (the floor host was
offline), so every other host carries a stand-in figure above the bar, in the line shape the pool probe prints.

The bar (10 GB) is the overlay's and is not touched. The card counts a host under the bar, a host with a figure it could
not read, a host the receipt holds no line for, and a host whose figure is older than the source's bound as not above it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelLabSourceRead,
    ModelLabStateInput,
    ModelLabStateOverlay,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    disk_expected_hosts,
    read_disk,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    iso,
    machine,
    overlay_text,
    record,
)

pytestmark = pytest.mark.unit

HOSTS = ("h051", "h053", "h101", "h105", "h106", "h201", "h202")
STAND_IN_FREE = "50.0G"


def lab_overlay(**over: object) -> ModelLabStateOverlay:
    return ModelLabStateOverlay.model_validate(
        {**overlay_text("node_floor_simple_effect"), **over}
    )


def line(host: str, free: str = STAND_IN_FREE, extra: str = "") -> str:
    """One reading as the pool probe prints it: the volume cells the overlay names and, last, the cache age when the text is a kept one."""
    return (
        f"{host}:cores=32,load1=4.0,"
        f"disk_lanetmp={free}/545.9G/47%,disk_workdir={free}/545.9G/45%,placed=0,cap=5{extra}"
    )


def receipt(
    tmp_path: Path, readings: list[str], age: timedelta = timedelta(minutes=1)
) -> Path:
    path = tmp_path / "latest.json"
    path.write_text(
        json.dumps({"started_at": iso(NOW - age), "readings": readings}),
        encoding="utf-8",
    )
    return path


def recorded_21_16(h053_free: str) -> list[str]:
    return [line(h, h053_free if h == "h053" else STAND_IN_FREE) for h in HOSTS]


def card(read: ModelLabSourceRead, ov: ModelLabStateOverlay):
    """The card as the floor draws it: the lab-state verdict of the disk source."""
    out = HandlerLabState().handle(
        ModelLabStateInput(now=NOW, sources=ov.sources, reads=(read,))
    )
    return next(v for v in out.verdicts if v.kind == "disk")


@pytest.mark.parametrize("h053_free", ["7.6G", "3.7G"])
def test_golden_the_recorded_h053_figure_under_the_bar_reads_six_of_seven(
    tmp_path: Path, h053_free: str
) -> None:
    ov = lab_overlay()
    read = read_disk(receipt(tmp_path, recorded_21_16(h053_free)), ov, NOW)
    assert read.summary == "6 of 7 hosts above 10 GB free"
    h053 = next(s for s in read.subjects if s.name == "h053")
    assert not h053.ok
    assert "under the 10 GB floor" in h053.note
    verdict = card(read, ov)
    assert verdict.state == "NOT WORKING"
    assert "h053" in verdict.reason


def test_positive_control_every_host_above_the_bar_reads_seven_of_seven(
    tmp_path: Path,
) -> None:
    ov = lab_overlay()
    read = read_disk(receipt(tmp_path, recorded_21_16("98.0G")), ov, NOW, HOSTS)
    assert read.summary == "7 of 7 hosts above 10 GB free"
    assert all(s.ok for s in read.subjects)
    assert card(read, ov).state == "WORKING"


def test_the_bar_is_the_overlays_and_a_host_exactly_on_it_is_above_it(
    tmp_path: Path,
) -> None:
    ov = lab_overlay()
    read = read_disk(receipt(tmp_path, [line("h053", "10.0G")]), ov, NOW)
    assert read.summary == "1 of 1 hosts above 10 GB free"


def test_error_a_figure_the_probe_could_not_read_is_not_above_the_bar(
    tmp_path: Path,
) -> None:
    """Workdir is UNREAD on h053 while lanetmp reads 50 GB: the host used to count above the bar on lanetmp alone."""
    ov = lab_overlay()
    unread = (
        "h053:cores=32,load1=4.0,disk_lanetmp=50.0G/545.9G/47%,"
        "disk_workdir=UNREAD:df-failed,placed=0,cap=5"
    )
    readings = [line(h) for h in HOSTS if h != "h053"] + [unread]
    read = read_disk(receipt(tmp_path, readings), ov, NOW)
    assert read.summary == "6 of 7 hosts above 10 GB free"
    h053 = next(s for s in read.subjects if s.name == "h053")
    assert not h053.ok
    assert "disk_workdir unread" in h053.note


def test_error_a_host_the_receipt_holds_no_line_for_is_unread_not_absent(
    tmp_path: Path,
) -> None:
    ov = lab_overlay()
    ov_record = record()
    ov_record["floor"]["machines"] = [machine(h) for h in HOSTS]
    expected = disk_expected_hosts(ov_record, ov)
    assert expected == HOSTS
    readings = [line(h) for h in HOSTS if h != "h053"]
    read = read_disk(receipt(tmp_path, readings), ov, NOW, expected)
    assert read.summary == "6 of 7 hosts above 10 GB free"
    h053 = next(s for s in read.subjects if s.name == "h053")
    assert not h053.ok
    assert "no line in the pool receipt" in h053.note
    # Without the floor record's host list the card would have read the six it was given and said "6 of 6".
    assert (
        read_disk(receipt(tmp_path, readings), ov, NOW).summary
        == "6 of 6 hosts above 10 GB free"
    )


def test_expected_hosts_leave_out_declared_and_unprobed_hosts() -> None:
    ov = lab_overlay(disk_unprobed_hosts=["h200"])
    rec = record()
    rec["floor"]["machines"] = [
        machine("h051"),
        machine("h105", "out"),
        machine("h200"),
        machine("h202", "repair"),
    ]
    assert disk_expected_hosts(rec, ov) == ("h051",)
    assert disk_expected_hosts(None, ov) == ()


def test_error_a_figure_older_than_the_bound_is_not_above_the_bar(
    tmp_path: Path,
) -> None:
    """The receipt is a minute old, but h053's line is a kept text already 20 minutes old when the receipt was written."""
    ov = lab_overlay()
    readings = [line(h) for h in HOSTS if h != "h053"] + [
        line("h053", "98.0G", extra=",cached=1200s")
    ]
    read = read_disk(receipt(tmp_path, readings), ov, NOW)
    assert read.summary == "6 of 7 hosts above 10 GB free"
    h053 = next(s for s in read.subjects if s.name == "h053")
    assert not h053.ok
    assert "over the 900 s bound" in h053.note


def test_positive_control_a_kept_text_inside_the_bound_still_counts(
    tmp_path: Path,
) -> None:
    ov = lab_overlay()
    readings = [line(h) for h in HOSTS if h != "h053"] + [
        line("h053", "98.0G", extra=",cached=60s")
    ]
    read = read_disk(receipt(tmp_path, readings), ov, NOW)
    assert read.summary == "7 of 7 hosts above 10 GB free"


def test_error_a_receipt_older_than_the_bound_counts_no_host_above_the_bar(
    tmp_path: Path,
) -> None:
    ov = lab_overlay()
    path = receipt(tmp_path, recorded_21_16("98.0G"), age=timedelta(minutes=33))
    read = read_disk(path, ov, NOW)
    assert read.summary == "0 of 7 hosts above 10 GB free"
    assert card(read, ov).state == "NOT WORKING"
