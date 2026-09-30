# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20105 -- placement of a lab work unit from capacity advertisements."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.lab_work.placement import (
    EnumPlacementDecision,
    EnumRefusalReason,
    place,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityAdvertisement,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 29, 18, 0, 0, tzinfo=UTC)
GB = 1024**3


def _ad(
    host: str,
    load1: float,
    cores: int,
    *,
    free_gb: float = 32,
    age: float = 1,
    tools: tuple[str, ...] = ("git", "uv"),
    running: int = 0,
    max_units: int = 1,
    penalty: float = 0.0,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=cores,
        load1=load1,
        mem_available_bytes=int(free_gb * GB),
        tools=list(tools),
        running_units=running,
        max_units=max_units,
        rank_penalty=penalty,
        advertised_at=NOW - timedelta(seconds=age),
        cadence_seconds=10,
    )


def test_placement_picks_lowest_load_per_core_under_the_bar() -> None:
    ads = [
        _ad("h200", 63.5, 24),
        _ad("h202", 2.3, 32),
        _ad("h105", 2.2, 10),
        _ad("h201", 47.5, 32),
    ]
    result = place(ads, evaluated_at=NOW)
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h202"  # 0.07/core beats h105's 0.22
    reasons = {v.host_name: v.reason for v in result.verdicts}
    assert "over the 0.75 load/core bar" in reasons["h200"]
    assert "over the 0.75 load/core bar" in reasons["h201"]


def test_placement_puts_work_on_the_mac_when_its_load_is_low_enough() -> None:
    """Operator ruling 2026-09-29: the launching Mac is eligible under the bar."""
    ads = [_ad("h200", 2.0, 24), _ad("h105", 4.0, 10)]
    assert place(ads, evaluated_at=NOW).host_name == "h200"


def test_placement_staleness_is_checked_before_headroom() -> None:
    ads = [_ad("h202", 0.1, 32, age=25), _ad("h105", 5.0, 10)]
    result = place(ads, evaluated_at=NOW)
    assert result.host_name == "h105"
    stale = next(v for v in result.verdicts if v.host_name == "h202")
    assert stale.reason.startswith("stale")


def test_placement_refuses_a_future_dated_advertisement() -> None:
    result = place([_ad("h202", 0.1, 32, age=-60)], evaluated_at=NOW)
    assert result.refusal is EnumRefusalReason.COULD_NOT_CHECK


def test_placement_could_not_check_is_distinct_from_over_capacity() -> None:
    nothing = place([], evaluated_at=NOW)
    all_stale = place([_ad("h202", 0.1, 32, age=100)], evaluated_at=NOW)
    busy = place([_ad("h200", 70, 24), _ad("h201", 48, 32)], evaluated_at=NOW)
    assert nothing.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert all_stale.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert busy.refusal is EnumRefusalReason.OVER_CAPACITY
    assert busy.decision is EnumPlacementDecision.REFUSED


def test_placement_requires_the_tools_the_unit_needs() -> None:
    ads = [
        _ad("h202", 0.1, 32, tools=("git", "uv", "gh")),
        _ad("h105", 0.1, 10, tools=("crush",)),
    ]
    assert place(ads, evaluated_at=NOW, need_tools=("crush",)).host_name == "h105"
    missing = place(ads, evaluated_at=NOW, need_tools=("claude",))
    assert missing.refusal is EnumRefusalReason.MISSING_TOOLS


def test_placement_counts_running_units_and_slots() -> None:
    full = _ad("h202", 0.1, 32, running=1, max_units=1)
    assert place([full], evaluated_at=NOW).refusal is EnumRefusalReason.OVER_CAPACITY
    assert place([full, _ad("h105", 1.0, 10)], evaluated_at=NOW).host_name == "h105"


def test_placement_uses_the_newest_advertisement_per_host_and_is_deterministic() -> (
    None
):
    old = _ad("h202", 30.0, 32, age=5)
    new = _ad("h202", 0.5, 32, age=1)
    first = place([old, new, _ad("h105", 3.0, 10)], evaluated_at=NOW)
    second = place([new, _ad("h105", 3.0, 10), old], evaluated_at=NOW)
    assert first.host_name == "h202"
    assert first.model_dump_json() == second.model_dump_json()


def test_placement_threshold_override_and_only_host() -> None:
    ads = [_ad("h200", 30.0, 24), _ad("h105", 2.0, 10)]
    assert (
        place(ads, evaluated_at=NOW, only_host="h200").refusal
        is EnumRefusalReason.OVER_CAPACITY
    )
    loose = place(ads, evaluated_at=NOW, only_host="h200", max_load_per_core=2.0)
    assert loose.host_name == "h200"


def test_rank_penalty_ranks_last_resort_hosts_last_but_never_bars_them() -> None:
    evidence = _ad("h201", 3.2, 32, penalty=0.25)  # 0.10/core + 0.25
    idle_mac = _ad("h105", 2.0, 10)  # 0.20/core
    assert place([evidence, idle_mac], evaluated_at=NOW).host_name == "h105"
    alone = place([evidence], evaluated_at=NOW)
    assert alone.decision is EnumPlacementDecision.PLACED
    assert alone.host_name == "h201"
