# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 2026-11-23 failures nobody read get alarms from the failure alarm router (OMN-26216).

Replayed over a trimmed fixture of the 2026-11-23 ledger roll (``fixtures/rolling_ledger_2026_11_23_trim.txt``,
each row cut to the cells the router reads, the stamps and values as written): the delegation failures that began at 05:18Z, the pr-create refusals, and the dev
work-ledger query seam that read NOT_WORKING from 12:35Z. Each replay fires the router at a clock time over the rows
written by then, as the runtime tick would.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    HandlerFailureAlarmRouter,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
    ModelFailureAlarmResult,
)
from tests.nodes.node_failure_alarm_router_effect.helpers import FakePorts

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "rolling_ledger_2026_11_23_trim.txt"
CONTRACT = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_failure_alarm_router_effect/contract.yaml"
)
ROWS = FIXTURE.read_text(encoding="utf-8").splitlines()
ORCHESTRATOR = "orchestrator-31d9-swarm"
STREAK = "delegation-model-failing:claude-sonnet-5-5:provider_auth_failed"
SEAM = "seam-not-working:work-ledger-query"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def rows_until(now: datetime) -> list[str]:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [row for row in ROWS if row.split(" | ")[0] <= stamp]


def fire(now: datetime, ports: FakePorts | None = None) -> ModelFailureAlarmResult:
    ports = ports or FakePorts(rows_until(now))
    return HandlerFailureAlarmRouter(ports=ports).handle(
        ModelFailureAlarmRequest(now=now)
    )


def causes(result: ModelFailureAlarmResult) -> list[str]:
    return [alarm.cause for alarm in result.alarms]


def config() -> dict[str, object]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"][
        "failure_alarm_router"
    ]


def test_delegation_failure_replay_alarms_by_0530() -> None:
    """Four claude-sonnet-5-5 provider_auth_failed rows (05:18:28Z to 05:24:07Z) and no completed row of it alarm."""
    ports = FakePorts(rows_until(at("2026-11-23T05:30:00Z")))
    result = fire(at("2026-11-23T05:30:00Z"), ports)
    assert STREAK in causes(result)
    alarm = next(a for a in result.alarms if a.cause == STREAK)
    assert alarm.first_at == "2026-11-23T05:18:28Z"
    assert alarm.orchestrator == ORCHESTRATOR
    msgs = [
        row
        for _, rows in ports.appends
        for row in rows
        if f" | MSG | from=failure-alarm-router | to={ORCHESTRATOR} " in row
    ]
    assert len(msgs) == len(result.alarms)
    assert any("claude-sonnet-5-5" in row for row in msgs)


def test_delegation_alarm_waits_for_the_streak_and_ignores_other_models() -> None:
    """Before the fourth failed row, and with only glm completions around, no streak alarm exists."""
    before = fire(at("2026-11-23T05:23:00Z"))
    assert STREAK not in causes(before)
    assert not any(c.startswith("delegation-model-failing:glm") for c in causes(before))
    assert STREAK in causes(fire(at("2026-11-23T05:24:08Z")))


def test_refusal_alarm_within_ten_minutes_of_the_first_row_of_each_cause() -> None:
    first = {
        "reader-overlay-not-configured": "2026-11-23T03:32:46Z",
        "guard-unchecked": "2026-11-23T03:37:46Z",
        "reader-exit-69": "2026-11-23T08:18:50Z",
        "pr-create-timeout": "2026-11-23T12:51:06Z",
    }
    for cause, stamp in first.items():
        key = f"pr-create-refusal:{cause}"
        assert key not in causes(fire(at(stamp) - timedelta(seconds=1))), cause
        result = fire(at(stamp) + timedelta(minutes=10))
        assert key in causes(result), cause
        alarm = next(a for a in result.alarms if a.cause == key)
        assert alarm.first_at == stamp
        assert alarm.orchestrator == ORCHESTRATOR


def test_seam_not_working_alarm_within_ten_minutes_of_the_first_reading() -> None:
    """The work-ledger-query seam read NOT_WORKING at 12:35:24Z; the answerer stayed silent until 20:00Z."""
    ports = FakePorts(rows_until(at("2026-11-23T12:40:24Z")))
    result = fire(at("2026-11-23T12:40:24Z"), ports)
    assert SEAM in causes(result)
    alarm = next(a for a in result.alarms if a.cause == SEAM)
    assert alarm.first_at == "2026-11-23T12:35:24Z"
    assert "no-result-by-deadline" in alarm.kind
    assert any(
        f" | MSG | from=failure-alarm-router | to={ORCHESTRATOR} " in r
        for _, rows in ports.appends
        for r in rows
    )
    assert SEAM not in causes(fire(at("2026-11-23T12:35:23Z")))


def test_a_seam_drill_raises_one_message() -> None:
    drill_only = [
        row
        for row in ROWS
        if " | CLAIM | " in row or row.startswith("2026-11-23T12:50:24Z")
    ]
    ports = FakePorts(drill_only)
    result = fire(at("2026-11-23T12:55:00Z"), ports)
    assert causes(result) == [f"{SEAM}:drill@2026-11-23T12:50:24Z"]
    assert sum(" | MSG | " in row for _, rows in ports.appends for row in rows) == 1


def test_seam_over_budget_alarm_resolves_when_the_seam_answers_inside_the_budget() -> (
    None
):
    """19:42:23Z answered in 21514 ms; the contract budget is 10000 ms; 20:42:50Z answered in 7132 ms."""
    ports = FakePorts(rows_until(at("2026-11-23T19:45:00Z")))
    over = fire(at("2026-11-23T19:45:00Z"), ports)
    assert "seam-over-budget:work-ledger-query" in causes(over)
    alarm = next(
        a for a in over.alarms if a.cause == "seam-over-budget:work-ledger-query"
    )
    assert "21514" in alarm.kind
    assert "10000" in alarm.kind
    later = at("2026-11-23T20:45:00Z")
    ports.rows.extend(
        row for row in rows_until(later) if row.split(" | ")[0] > "2026-11-23T19:45:00Z"
    )
    back = fire(later, ports)
    assert "seam-over-budget:work-ledger-query" in [r.cause for r in back.resolved]


def test_the_latency_budget_is_the_contracts_value_not_the_handlers() -> None:
    assert _section("seam_deadline")["latency_budget_ms"] == 10000
    tight = HandlerFailureAlarmRouter(
        ports=FakePorts(rows_until(at("2026-11-23T20:45:00Z"))),
        rules=_rules_with(latency_budget_ms=5000),
    ).handle(ModelFailureAlarmRequest(now=at("2026-11-23T20:45:00Z")))
    assert "seam-over-budget:work-ledger-query" in [a.cause for a in tight.alarms]


def _section(name: str) -> dict[str, int]:
    section = config()[name]
    assert isinstance(section, dict)
    return section


def _rules_with(**seam: int):
    from omnimarket.nodes.node_failure_alarm_router_effect.rules import load_rules

    rules = load_rules()
    return rules.model_copy(
        update={"seam_deadline": rules.seam_deadline.model_copy(update=seam)}
    )


def test_the_cadence_is_declared_in_the_contract_and_the_router_is_wired_to_the_tick() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert _section("schedule")["interval_seconds"] == 300
    tick = "onex.intent.platform.runtime-tick.v1"
    assert tick in contract["event_bus"]["subscribe_topics"]
    assert any(
        h.get("topic") == tick
        and h["handler"]["name"] == "HandlerFailureAlarmRouterScheduled"
        for h in contract["handler_routing"]["handlers"]
    )


def test_the_scheduled_handler_fires_once_per_five_minute_window() -> None:
    from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
        HandlerFailureAlarmRouterScheduled,
    )

    now = at("2026-11-23T12:40:00Z")
    ports = FakePorts(rows_until(now))
    scheduled = HandlerFailureAlarmRouterScheduled(
        router=HandlerFailureAlarmRouter(ports=ports),
        request=ModelFailureAlarmRequest(),
    )

    def tick(when: datetime) -> ModelRuntimeTick:
        return ModelRuntimeTick(
            now=when,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=when,
            correlation_id=uuid4(),
            scheduler_id="test",
            tick_interval_ms=1000,
        )

    first = asyncio.run(scheduled.handle(tick(now)))
    between = asyncio.run(scheduled.handle(tick(now + timedelta(seconds=60))))
    assert first is not None
    assert SEAM in causes(first)
    assert between is None
