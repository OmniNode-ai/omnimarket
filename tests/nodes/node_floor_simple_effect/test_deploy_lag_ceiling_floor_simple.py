# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy lag ceiling: a merged change that waits past the ceiling reads NOT WORKING (D12, 2026-11-12T15:51:18Z).

The 2026-11-12 stall: no merge after 07:06Z ran for over five hours, yet the deploy_lag row read WORKING under the 1440-minute
limit. The ceiling of 150 minutes (first value, D12) makes a 151-minute wait read NOT WORKING while a 149-minute wait does not.

Injected breaks, each a test below: a merge that is running or whose reason is not not_yet_deployed never counts; an overlay
without the ceiling key still loads (ceiling is None); a ceiling of 0 is refused by validation.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.floor_17a import ModelLabStateOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import read_deploy_lag
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    overlay_text,
    record,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit


def _overlay(**over: Any) -> ModelLabStateOverlay:
    loaded = overlay_text("node_floor_simple_effect")
    loaded.update(over)
    return ModelLabStateOverlay.model_validate(loaded)


def _merged(
    minutes_ago: int, running: bool = False, reason: str = "not_yet_deployed"
) -> dict[str, Any]:
    return {
        "floor": {
            "merged_running": {
                "generated_at": NOW.isoformat(),
                "days": [
                    {
                        "merges": [
                            {
                                "repo": "omnimarket",
                                "number": 5140,
                                "merged_at": (
                                    NOW - timedelta(minutes=minutes_ago)
                                ).isoformat(),
                                "running": running,
                                "reason": reason,
                            }
                        ]
                    }
                ],
            }
        }
    }


def _floor_with_one_merge(minutes_ago: int) -> dict[str, Any]:
    rec = record()
    rec["floor"]["merged_running"]["days"][0]["merges"][0].update(
        {
            "merged_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "running": False,
            "reason": "not_yet_deployed",
        }
    )
    return rec


def test_golden_the_fixture_overlay_carries_the_ceiling_value() -> None:
    assert _overlay().deploy_lag_ceiling_minutes == 150


def test_golden_a_149_minute_merge_reads_ok_and_151_reads_not_ok_with_the_ceiling_note() -> (
    None
):
    overlay = _overlay()
    ok = read_deploy_lag(_merged(149), NOW, overlay)
    assert ok.status == "read"
    assert [s.ok for s in ok.subjects] == [True]
    edge = read_deploy_lag(_merged(150), NOW, overlay)
    assert [s.ok for s in edge.subjects] == [True]
    red = read_deploy_lag(_merged(151), NOW, overlay)
    assert [s.ok for s in red.subjects] == [False]
    assert (
        red.subjects[0].note == "merged, not running, 151 min, over the 150 min ceiling"
    )


def test_golden_through_the_tick_a_151_minute_merge_draws_not_working_and_red_and_149_does_not(
    tmp_path: Path,
) -> None:
    red_home = build_home(tmp_path / "red", rec=_floor_with_one_merge(151))
    assert run(red_home).ok
    red = next(x for x in written(red_home).lab_state if x.id == "deploy_lag")
    assert (red.state, red.draw) == ("NOT WORKING", "red")
    ok_home = build_home(tmp_path / "ok", rec=_floor_with_one_merge(149))
    assert run(ok_home).ok
    ok = next(x for x in written(ok_home).lab_state if x.id == "deploy_lag")
    assert (ok.state, ok.draw) == ("WORKING", "green")


def test_error_an_overlay_without_the_ceiling_key_loads_as_none_and_the_limit_alone_decides() -> (
    None
):
    loaded = overlay_text("node_floor_simple_effect")
    del loaded["deploy_lag_ceiling_minutes"]
    overlay = ModelLabStateOverlay.model_validate(loaded)
    assert overlay.deploy_lag_ceiling_minutes is None
    assert [s.ok for s in read_deploy_lag(_merged(773), NOW, overlay).subjects] == [
        True
    ]
    over_limit = read_deploy_lag(_merged(1441), NOW, overlay)
    assert (
        over_limit.subjects[0].note
        == "merged, not running, 1441 min, over the 1440 min limit"
    )


def test_error_a_ceiling_of_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        _overlay(deploy_lag_ceiling_minutes=0)


def test_error_a_running_merge_or_another_reason_never_counts_however_old() -> None:
    overlay = _overlay()
    assert read_deploy_lag(_merged(2000, running=True), NOW, overlay).subjects == ()
    assert read_deploy_lag(_merged(2000, reason="adopted"), NOW, overlay).subjects == ()
