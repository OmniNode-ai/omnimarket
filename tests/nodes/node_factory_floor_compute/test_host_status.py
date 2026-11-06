# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Machines tab's per-host record (OMN-22673): a golden read, an error read, a stale read and a failing control.

The golden case parses a recorded pool read (fixtures/pool_read_2026-11-06T1416Z.json, the cells the record reads), so the
numbers are the ones the lab printed, not values imagined here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorDriftHost,
    ModelFloorHostDrift,
    ModelFloorHostRow,
    ModelFloorLedgerRow,
    ModelFloorMachineDecl,
    ModelFloorPlacedHost,
    ModelFloorPoolReading,
)
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_factory_floor_compute.handlers import floor_host_status
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorHostStatus,
    ModelHostFlag,
    ModelHostNumber,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_pool_reading,
)
from tests.nodes.node_factory_floor_compute.support import (
    advertisement,
    ago,
    request,
    row,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "pool_read_2026-11-06T1416Z.json"
TABLE = (
    ModelFloorHostRow(host="h053", cap=16),
    ModelFloorHostRow(host="h201", cap=3),
    ModelFloorHostRow(host="h101", cap=12),
    ModelFloorHostRow(
        host="h200",
        cap=1,
        available=False,
        unavailable_reason="out of lane placement: no lanes on the orchestrator Mac",
    ),
    ModelFloorHostRow(host="h106", cap=1),
)
MACHINES = (
    ModelFloorMachineDecl(host="h053", label="h053"),
    ModelFloorMachineDecl(host="h200", label="h200 (the Mac)"),
)


def recorded_pool(age_minutes: float = 2) -> tuple[ModelFloorPoolReading, ...]:
    lines = json.loads(FIXTURE.read_text(encoding="utf-8"))["readings"]
    return tuple(parse_pool_reading(line, ago(age_minutes)) for line in lines)


def lane_rows() -> tuple[ModelFloorLedgerRow, ...]:
    return (
        row(
            "CLAIM",
            70,
            "floor-build",
            tickets=("OMN-22478",),
            text="build the floor",
            host="h053",
        ),
        row(
            "CLAIM",
            9,
            "landing-L28625",
            tickets=("OMN-19998",),
            text="land a pull request",
            host="h053",
            actor="script:landing-controller",
        ),
        row("CLAIM", 5, "elsewhere", tickets=("OMN-1",), text="work", host="h201"),
    )


def advert(
    host: str, *tools: str, age_seconds: int = 60
) -> ModelHostCapacityAdvertisement:
    """The shared advertisement builder with the tools the host advertised."""
    return advertisement(host, running=0, slots=2, age_seconds=age_seconds).model_copy(
        update={"tools": list(tools)}
    )


def statuses(**extra: Any) -> dict[str, ModelFloorHostStatus]:
    req = request(machines=MACHINES, ledger=lane_rows(), **extra)
    result = compute_factory_floor(req)
    return {s.host: s for s in result.floor.host_status}


def full_facts(**override: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "host_table": TABLE,
        "pool": recorded_pool(),
        "placed": (ModelFloorPlacedHost(host="h053", units=4, lanes=1),),
        "placement": (
            advert("h053", "claude", "docker", "git"),
            advert("h201", "claude", "git"),
        ),
        "host_drift": ModelFloorHostDrift(
            folded_at=ago(1),
            hosts=(
                ModelFloorDriftHost(
                    host="h053", last_heartbeat_at=ago(3), open_items=0
                ),
                ModelFloorDriftHost(
                    host="h201", last_heartbeat_at=ago(2), open_items=8
                ),
                ModelFloorDriftHost(host="h101", last_heartbeat_at=None, open_items=0),
            ),
        ),
    }
    facts.update(override)
    return facts


def cells(status: ModelFloorHostStatus) -> list[ModelHostNumber | ModelHostFlag]:
    """Every value cell of a host's status."""
    return [
        status.cap,
        status.slots_used,
        status.slots_free,
        status.other_units,
        status.load_per_core,
        status.mem_free_gb,
        status.mem_pressure,
        status.swap_used_pct,
        status.disk_free_gb,
        status.disk_used_pct,
        status.heartbeat_age_min,
        status.drift_open,
        status.docker,
        status.reading_age,
        status.available,
    ]


def test_the_record_has_one_status_per_host_of_the_host_table() -> None:
    found = statuses(**full_facts())
    assert sorted(found) == ["h053", "h101", "h106", "h200", "h201"]


def test_golden_a_fresh_host_reads_every_value_from_its_own_source() -> None:
    h = statuses(**full_facts())["h053"]
    assert (h.condition, h.available.value, h.cap.value, h.cap.source) == (
        "read",
        True,
        16.0,
        "host table",
    )
    assert (h.slots_used.value, h.other_units.value, h.slots_used.source) == (
        4.0,
        3.0,
        "slot markers",
    )
    assert (h.slots_free.value, h.slots_free.source) == (16.0, "pool read")
    assert h.load_per_core.value == round(15.1 / 56, 2)
    assert h.load_per_core.state == "read"
    assert (h.mem_free_gb.value, h.mem_pressure.value, h.mem_pressure.unit) == (
        86.3,
        0.0,
        "psi",
    )
    assert (h.swap_used_pct.value, h.disk_free_gb.value) == (6.0, 39.1)
    assert h.disk_used_pct.value == round(100 * (64.0 - 39.1) / 64.0, 2)
    assert (h.heartbeat_age_min.value, h.heartbeat_age_min.state) == (3.0, "read")
    assert (h.drift_open.value, h.docker.value, h.docker.state) == (0.0, True, "read")
    assert (h.reading_age.value, h.reading_age.state) == (2.0, "read")
    assert [u.lane for u in h.lanes] == ["floor-build"]
    assert [(u.lane, u.ticket) for u in h.landing_workers] == [
        ("landing-L28625", "OMN-19998")
    ]
    assert h.lanes[0].run_minutes == 70
    assert all(c.state == "read" for c in cells(h))


def test_golden_the_macos_pressure_level_and_a_refusing_host_are_read_as_such() -> None:
    h = statuses(**full_facts())["h101"]
    assert (h.mem_pressure.value, h.mem_pressure.unit) == (1.0, "level")
    assert h.condition == "refusing"
    assert h.admission_refusal.startswith("HOOK-PARSE-UNKNOWN")
    assert h.heartbeat_age_min.state == "unread"
    assert "never reported" in h.heartbeat_age_min.reason


def test_golden_a_host_out_of_selection_carries_the_table_reason_and_reads_nothing_else_as_a_number() -> (
    None
):
    h = statuses(**full_facts())["h200"]
    assert (h.condition, h.available.value) == ("out", False)
    assert (
        h.unavailable_reason
        == "out of lane placement: no lanes on the orchestrator Mac"
    )
    assert h.load_per_core.state == "unread"
    assert "out of selection" in h.load_per_core.reason
    assert h.docker.state == "unread"
    assert "no capacity advertisement" in h.docker.reason
    assert (
        h.heartbeat_age_min.reason
        == "the host-drift fleet projection holds no row for this host"
    )


def test_golden_a_host_whose_advertisement_lacks_the_docker_tool_reads_no_docker() -> (
    None
):
    h = statuses(**full_facts())["h201"]
    assert (h.docker.state, h.docker.value) == ("read", False)


def test_error_every_missing_source_is_unread_with_its_reason() -> None:
    found = statuses(host_table=TABLE)
    for status in found.values():
        assert status.slots_used.reason == "the slot markers were not read"
        assert status.load_per_core.reason == "the pool read was not recorded"
        assert (
            status.heartbeat_age_min.reason
            == "the host-drift fleet projection was not read"
        )
        assert status.docker.state == "unread"
        assert status.condition in {"out", "unread"}
        assert [
            c
            for c in cells(status)
            if c.state == "read" and c is not status.available and c is not status.cap
        ] == []


def test_error_an_unread_host_table_leaves_availability_unread_and_never_available() -> (
    None
):
    found = statuses(**full_facts(host_table=None))
    assert sorted(found) == ["h053", "h200"]
    for status in found.values():
        assert status.available.state == "unread"
        assert status.available.value is None
        assert status.available.reason == "the host table was not read"


def test_error_a_pool_read_that_failed_for_one_host_names_the_error() -> None:
    bad = ModelFloorPoolReading(
        host="h053", read_at=ago(1), error="ssh: connect timed out"
    )
    h = statuses(**full_facts(pool=(bad,)))["h053"]
    assert h.condition == "unread"
    assert (
        h.load_per_core.reason
        == "the pool read could not read this host: ssh: connect timed out"
    )
    assert h.reading_age.state == "unread"


def test_error_a_value_the_reading_has_no_cell_for_is_unread_not_zero() -> None:
    bare = ModelFloorPoolReading(host="h053", read_at=ago(1), cores=8, load1=1.0)
    h = statuses(**full_facts(pool=(bare,)))["h053"]
    assert h.mem_free_gb.state == "unread"
    assert h.mem_free_gb.value is None
    assert h.disk_free_gb.reason == "the pool reading carries no disk_runs cell"
    assert h.mem_pressure.state == "unread"


def stale_facts() -> dict[str, object]:
    return full_facts(
        pool=recorded_pool(age_minutes=45),
        placement=(
            advert(
                "h053", "docker", age_seconds=int(timedelta(minutes=20).total_seconds())
            ),
        ),
        host_drift=ModelFloorHostDrift(
            folded_at=ago(1),
            hosts=(
                ModelFloorDriftHost(
                    host="h053", last_heartbeat_at=ago(130), open_items=0
                ),
            ),
        ),
    )


def test_stale_a_reading_past_its_bound_is_stale_and_the_host_is_never_read() -> None:
    h = statuses(**stale_facts())["h053"]
    assert h.condition == "stale"
    assert (h.load_per_core.state, h.disk_free_gb.state, h.reading_age.state) == (
        "stale",
        "stale",
        "stale",
    )
    assert h.reading_age.value == 45.0
    assert "bound is 30 min" in h.reading_age.reason
    assert (h.docker.state, h.docker.value) == ("stale", True)
    assert h.heartbeat_age_min.state == "stale"
    assert h.heartbeat_age_min.value == 130.0
    assert_no_cell_reads_fresh_except_live(h)


def assert_no_cell_reads_fresh_except_live(status: ModelFloorHostStatus) -> None:
    """Table and marker cells are read live each cycle; every cell of an aged source must not say read."""
    aged: list[ModelHostNumber | ModelHostFlag] = [
        status.slots_free,
        status.load_per_core,
        status.mem_free_gb,
        status.mem_pressure,
        status.swap_used_pct,
        status.disk_free_gb,
        status.disk_used_pct,
        status.heartbeat_age_min,
        status.docker,
        status.reading_age,
    ]
    assert status.condition != "read"
    assert [c.state for c in aged if c.state == "read"] == []


def test_stale_a_fleet_projection_folded_too_long_ago_makes_every_heartbeat_figure_stale() -> (
    None
):
    old = ModelFloorHostDrift(
        folded_at=ago(130),
        hosts=(
            ModelFloorDriftHost(host="h053", last_heartbeat_at=ago(131), open_items=2),
        ),
    )
    h = statuses(**full_facts(host_drift=old))["h053"]
    assert (h.heartbeat_age_min.state, h.drift_open.state) == ("stale", "stale")
    assert "folded 130 min ago" in h.heartbeat_age_min.reason


def test_failing_control_an_injected_break_that_never_judges_age_turns_the_stale_check_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named break: ``_judge`` returns ``read`` whatever the age. The same stale check must then fail."""
    h = statuses(**stale_facts())["h053"]
    assert_no_cell_reads_fresh_except_live(h)  # green on the real code

    def never_stale(
        now: datetime, read_at: datetime, bound_seconds: int, source: str
    ) -> tuple[str, int, str]:
        return "read", int((now - read_at).total_seconds() // 60), ""

    monkeypatch.setattr(floor_host_status, "_judge", never_stale)
    broken = statuses(**stale_facts())["h053"]
    with pytest.raises(AssertionError):
        assert_no_cell_reads_fresh_except_live(broken)
