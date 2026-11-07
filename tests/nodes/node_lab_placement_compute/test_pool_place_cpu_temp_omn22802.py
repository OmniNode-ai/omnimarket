# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pool placement holds new heavy work on a host whose CPU reads above its hold bar (OMN-22803).

On 2026-11-06 h053 reached 90 C against the floor's 85 C bar with three model checks running at once.
Placement judged load and memory only. The golden test places a cool host, the error tests refuse a hot
one with a reason that names the temperature and the bar, and the last test replays the day's readings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_placement_compute.handlers import HandlerLabPoolPlace
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    EnumRefusalReason,
    ModelLabPoolPlaceRequest,
    ModelPlacement,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    CPU_TEMP_TOOL_PREFIX,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 6, 19, 0, 0, tzinfo=UTC)
GB = 1024**3
BAR = 80.0


def _ad(host: str, temp: float | None) -> ModelHostCapacityAdvertisement:
    tools = ["git", "uv"] + (
        [] if temp is None else [f"{CPU_TEMP_TOOL_PREFIX}{temp:.1f}"]
    )
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=32,
        load1=1.0,
        mem_available_bytes=40 * GB,
        tools=tools,
        advertised_at=NOW - timedelta(seconds=1),
        cadence_seconds=10,
    )


def _place(
    ads: list[ModelHostCapacityAdvertisement], bar: float | None = BAR
) -> ModelPlacement:
    table = ModelLabHostsTable(
        hosts=tuple(
            ModelLabHostRow(name=ad.host_name, target=ad.host_name, max_cpu_c=bar)
            for ad in ads
        )
    )
    return HandlerLabPoolPlace(environ={}).handle(
        ModelLabPoolPlaceRequest(
            advertisements=tuple(ads), evaluated_at=NOW, table=table
        )
    )


def test_a_cool_host_is_placed() -> None:
    result = _place([_ad("h053", 63.0)])
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h053"


def test_a_hot_host_is_refused_naming_the_temperature_and_the_bar() -> None:
    result = _place([_ad("h053", 90.0)])
    assert result.decision is EnumPlacementDecision.REFUSED
    assert result.refusal is EnumRefusalReason.OVER_CAPACITY
    reason = result.verdicts[0].reason
    assert "hot" in reason
    assert "90.0 C" in reason
    assert "80.0 C" in reason


def test_a_host_at_the_bar_is_placed() -> None:
    assert _place([_ad("h053", 80.0)]).decision is EnumPlacementDecision.PLACED


def test_a_host_with_no_reading_is_judged_on_load_and_memory() -> None:
    assert _place([_ad("h101", None)]).decision is EnumPlacementDecision.PLACED


def test_a_host_with_no_bar_is_not_judged_on_temperature() -> None:
    assert (
        _place([_ad("h053", 95.0)], bar=None).decision is EnumPlacementDecision.PLACED
    )


def test_a_hot_host_does_not_take_the_unit_a_cool_one_can() -> None:
    result = _place([_ad("h053", 90.0), _ad("h202", 55.0)])
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h202"


def test_todays_readings() -> None:
    """The failing control built from 2026-11-06: 90.0 and 87.3 C are refused, 63.0 and 75.0 C are placed."""
    readings = {"h053": [90.0, 63.0], "h201": [87.3, 75.0]}
    outcome = {
        (host, temp): _place([_ad(host, temp)]).decision
        for host, temps in readings.items()
        for temp in temps
    }
    assert outcome == {
        ("h053", 90.0): EnumPlacementDecision.REFUSED,
        ("h053", 63.0): EnumPlacementDecision.PLACED,
        ("h201", 87.3): EnumPlacementDecision.REFUSED,
        ("h201", 75.0): EnumPlacementDecision.PLACED,
    }
