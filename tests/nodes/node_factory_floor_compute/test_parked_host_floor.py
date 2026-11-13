# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host the lab host table parks (available false, not in repair) is neither a bay nor a heartbeat failure (OMN-19998).

GOLDEN: a parked host draws no bay at all, and its heartbeat cells are unread with the table's own reason — no
heartbeat is expected from a parked host. CONTROL: a machine down for repair keeps its stale bay, and an available
host whose projection row carries no heartbeat still says it has never reported one. ERROR: an unread host table
parks nobody: every declared machine draws its bay and no host is read as parked.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorDriftHost,
    ModelFloorHostDrift,
    ModelFloorHostRow,
    ModelFloorMachineDecl,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    ago,
    request,
)

pytestmark = pytest.mark.unit

MACHINES = (
    ModelFloorMachineDecl(host="h051", label="h051"),
    ModelFloorMachineDecl(host="h202", label="h202"),
    ModelFloorMachineDecl(host="h101", label="h101"),
)

PARKED = "parked, operator 2026-11-13"


def floor(**extra: Any) -> Any:
    return compute_factory_floor(request(machines=MACHINES, **extra)).floor


def test_golden_a_parked_host_draws_no_bay_and_its_heartbeat_is_unread_not_a_failure() -> (
    None
):
    result = floor(
        host_table=(
            ModelFloorHostRow(
                host="h202",
                available=False,
                unavailable_reason=PARKED,
            ),
        ),
    )
    assert {b.host for b in result.bays} == {"h051", "h101"}
    parked = {s.host: s for s in result.host_status}["h202"]
    assert (parked.condition, parked.unavailable_reason) == ("out", PARKED)
    assert (parked.heartbeat_age_min.state, parked.heartbeat_age_min.source) == (
        "unread",
        "host-drift fleet projection",
    )
    assert parked.heartbeat_age_min.reason == (
        f"out of selection ({PARKED}); no heartbeat is expected from this host"
    )
    assert "never reported" not in parked.heartbeat_age_min.reason
    assert (parked.drift_open.state, parked.drift_open.reason) == (
        "unread",
        parked.heartbeat_age_min.reason,
    )


def test_control_a_host_down_for_repair_keeps_its_stale_bay() -> None:
    result = floor(
        host_table=(
            ModelFloorHostRow(
                host="h202",
                available=False,
                unavailable_reason="down for repair 2026-10-23",
            ),
        ),
    )
    machine = next(m for m in result.machines if m.host == "h202")
    bay = next(b for b in result.bays if b.host == "h202")
    assert (machine.state, bay.stale) == ("repair", True)


def test_control_an_available_host_with_no_heartbeat_still_reports_the_failure() -> (
    None
):
    result = floor(
        host_table=(ModelFloorHostRow(host="h051"),),
        host_drift=ModelFloorHostDrift(
            folded_at=ago(1),
            hosts=(
                ModelFloorDriftHost(host="h051", last_heartbeat_at=None, open_items=0),
            ),
        ),
    )
    live = {s.host: s for s in result.host_status}["h051"]
    assert live.condition != "out"
    assert next(b for b in result.bays if b.host == "h051").stale is True
    assert live.heartbeat_age_min.state == "unread"
    assert live.heartbeat_age_min.reason == "this host has never reported a heartbeat"


def test_error_an_unread_host_table_parks_nobody_and_draws_every_declared_bay() -> None:
    result = floor(host_table=None)
    assert {b.host for b in result.bays} == {m.host for m in MACHINES}
    assert {s.host: s.condition for s in result.host_status} == {
        m.host: "unread" for m in MACHINES
    }
    assert all(
        "no heartbeat is expected" not in s.heartbeat_age_min.reason
        for s in result.host_status
    )
