# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the silent-failure alarm compute node."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    HandlerPlacementShareAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def _run(key: str, dispatched: int, placed: int, minutes_ago: int) -> ModelPlacementRun:
    return ModelPlacementRun(
        run_key=key,
        stamp=NOW - timedelta(minutes=minutes_ago),
        dispatched=dispatched,
        placed=placed,
    )


def test_golden_chain_two_runs_under_half_alarm_with_friction_cells() -> None:
    request = ModelPlacementShareRequest(
        now=NOW,
        runs=(_run("a", 4, 0, 80), _run("b", 4, 1, 60)),
        refusals=tuple(
            ModelLaneRefusal(run_key="b", lane=f"lane{i}", status="repo-unresolved")
            for i in range(3)
        ),
    )
    result = HandlerPlacementShareAlarm().handle(request)
    assert result.alarm
    assert result.reason == "repo-unresolved"
    assert "FRICTION" in result.friction_cells
    assert result.alert_text


def test_golden_chain_a_healthy_run_carries_no_rows() -> None:
    request = ModelPlacementShareRequest(now=NOW, runs=(_run("a", 3, 3, 20),))
    result = HandlerPlacementShareAlarm().handle(request)
    assert not result.alarm
    assert result.friction_cells == ""
    assert result.alert_text == ""


def test_error_chain_a_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelPlacementShareRequest.model_validate({"now": "not a time", "runs": "none"})
    assert {err["loc"][0] for err in caught.value.errors()} >= {"now", "runs"}
