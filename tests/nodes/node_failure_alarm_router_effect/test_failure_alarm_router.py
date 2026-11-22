# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One alarm per cause reaches the live orchestrator inbox and the operator push channel (OMN-26483).

Failure rows were written and nothing read them (2026-11-22: 112 identical MSGs to a lane name that did
not exist, 0 ACKs, a controller refusing every tick for about an hour with the kind only in ticks.jsonl).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    HandlerFailureAlarmRouter,
    PositiveControlFailedError,
    SelfCheckFailedError,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
)
from tests.nodes.node_failure_alarm_router_effect.helpers import (
    NOW,
    ORCHESTRATOR,
    FakePorts,
    base_rows,
    orchestrator_claim,
)

pytestmark = pytest.mark.unit

REFUSED_TERMINAL = (
    "2026-11-22T18:00:00Z | TERMINAL | lane=c16-1450 | ticket=OMN-26122 | outcome=not-started"
    " | the runner stopped after 45 s with status=ledger-refused exit 70"
)
RED_MAIN = (
    "2026-11-22T18:10:00Z | STATUS | lane=red-main-alarm | actor=node:node_red_main_alarm_effect"
    " | model=none | src=alarm | alarm=main-red | repo=OmniNode-ai/overlays | branch=main"
    " | head=a6016a3c184181a7fd1e3e9dfd0bfe9e5a175500 | check=ci | conclusion=cancelled"
    " | first_red_at=2026-11-22T18:10:00Z | owner=orchestrator-31d9-swarm | Required default-branch check is red"
)


def _fire(ports: FakePorts, **extra: object):
    request = ModelFailureAlarmRequest(now=NOW, **extra)
    return HandlerFailureAlarmRouter(ports=ports).handle(request)


def _msgs(ports: FakePorts) -> list[str]:
    return [row for _, rows in ports.appends for row in rows if " | MSG | " in row]


def test_ledger_refused_lane_alarm_reaches_both_destinations_once() -> None:
    ports = FakePorts([*base_rows(), REFUSED_TERMINAL])
    first = _fire(ports)
    second = _fire(ports)
    assert [a.cause for a in first.alarms] == ["ledger-refused:c16-1450"]
    assert second.alarms == ()
    assert len(ports.pushed) == 1
    msgs = _msgs(ports)
    assert len(msgs) == 1
    row = parse_row(msgs[0])
    assert row is not None
    assert row.value("to") == ORCHESTRATOR


def test_orchestrator_is_resolved_from_live_claims_not_a_fixed_name() -> None:
    rows = [
        orchestrator_claim("orchestrator-old-1111", "2026-11-22T06:00:00Z"),
        "2026-11-22T07:00:00Z | TERMINAL | lane=orchestrator-old-1111 | closes-CLAIM=2026-11-22T06:00:00Z"
        " | outcome=done | ended",
        orchestrator_claim("orchestrator-new-2222", "2026-11-22T07:30:00Z"),
        REFUSED_TERMINAL,
    ]
    ports = FakePorts(rows)
    _fire(ports)
    row = parse_row(_msgs(ports)[0])
    assert row is not None
    assert row.value("to") == "orchestrator-new-2222"


def test_no_live_orchestrator_is_an_alarm_to_the_operator_push_never_a_drop() -> None:
    ports = FakePorts(
        [
            "2026-11-22T18:30:00Z | CLAIM | lane=other-lane | actor=x | model=y | ticket=OMN-1"
            " | est ~1 lane-hours; displaces z; (OMN-1) | other",
            REFUSED_TERMINAL,
        ]
    )
    result = _fire(ports)
    assert _msgs(ports) == []
    assert len(result.alarms) == 1
    assert result.alarms[0].orchestrator is None
    assert len(ports.pushed) == 1
    assert "no live orchestrator lane" in ports.pushed[0]


def test_red_main_rows_are_read_not_redetected() -> None:
    ports = FakePorts([*base_rows(), RED_MAIN])
    result = _fire(ports)
    assert [a.failure_class for a in result.alarms] == ["main-red"]
    assert "OmniNode-ai/overlays" in result.alarms[0].subject
    assert len(ports.pushed) == 1
    assert len(_msgs(ports)) == 1


def test_router_ignores_its_own_alarm_rows() -> None:
    ports = FakePorts([*base_rows(), REFUSED_TERMINAL])
    _fire(ports)
    again = _fire(ports)
    # The alarm row's own text names ledger-refused; it must not raise a second cause.
    assert again.alarms == ()
    assert len(ports.pushed) == 1


def test_held_roll_command_is_a_typed_gap_and_other_classes_still_run() -> None:
    ports = FakePorts([*base_rows(), REFUSED_TERMINAL])
    result = _fire(ports)
    assert [g.failure_class for g in result.gaps] == ["held-roll-command"]
    assert "ledger rows" in result.gaps[0].reason
    assert len(result.alarms) == 1


def test_positive_control_zero_rows_parsed_of_nonempty_ledger_refuses_loudly() -> None:
    ports = FakePorts(["this is not a ledger row", "neither is this"])
    with pytest.raises(PositiveControlFailedError, match="zero ledger rows parsed"):
        _fire(ports)
    assert len(ports.pushed) == 1
    assert "positive control failed" in ports.pushed[0]


def test_positive_control_empty_ledger_refuses_loudly() -> None:
    with pytest.raises(PositiveControlFailedError):
        _fire(FakePorts([]))


def test_self_check_feeds_a_synthetic_failure_and_proves_both_destinations() -> None:
    ports = FakePorts([*base_rows()])
    router = HandlerFailureAlarmRouter(ports=ports)
    router.self_check()
    # The synthetic run uses its own in-memory sinks and leaves the real ones untouched.
    assert ports.pushed == []
    assert ports.appends == []


def test_self_check_failure_refuses_the_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnimarket.nodes.node_failure_alarm_router_effect.handler as module

    monkeypatch.setattr(module, "_deliver_message", lambda *_args, **_kwargs: None)
    with pytest.raises(SelfCheckFailedError):
        _fire(FakePorts([*base_rows(), REFUSED_TERMINAL]))


def test_push_channel_is_the_alert_command_field() -> None:
    from omnimarket.nodes.node_failure_alarm_router_effect.handler import CommandPorts

    ports = CommandPorts(
        ModelFailureAlarmRequest(
            now=NOW, alert_command="echo pushed", ledger_rows_file="x"
        )
    )
    ports.push("hello")  # runs `echo pushed hello`; a non-zero exit would raise


def test_push_failure_is_reported_and_the_cause_is_retried_next_fire() -> None:
    ports = FakePorts([*base_rows(), REFUSED_TERMINAL], push_error=OSError("down"))
    result = _fire(ports)
    assert result.alarms == ()
    assert result.failures
    assert ports.appends == []
    ports._push_error = None
    assert len(_fire(ports).alarms) == 1


def test_lookback_ignores_a_refusal_older_than_the_window() -> None:
    old = REFUSED_TERMINAL.replace("2026-11-22T18:00:00Z", "2026-11-20T18:00:00Z")
    result = _fire(FakePorts([*base_rows(), old]), lookback_hours=6)
    assert result.alarms == ()
    assert (NOW - timedelta(hours=6)).year == 2026


def test_orchestrator_with_no_claim_of_its_own_is_resolved_from_live_claims_naming_it_parent() -> (
    None
):
    """The 2026-11-22 ledger holds no CLAIM for the session lane; its open child CLAIMs name it as parent."""
    rows = [
        "2026-11-22T22:00:00Z | CLAIM | lane=child-lane-1 | actor=x | model=y | ticket=OMN-1"
        " | est ~1 lane-hours; displaces z; (OMN-1) | parent=orchestrator-31d9-swarm | child",
        REFUSED_TERMINAL,
    ]
    ports = FakePorts(rows)
    result = _fire(ports)
    row = parse_row(_msgs(ports)[0])
    assert row is not None
    assert row.value("to") == ORCHESTRATOR
    assert result.alarms[0].orchestrator_source == "claim-parent"


def test_orchestrator_named_as_parent_only_by_closed_claims_is_not_live() -> None:
    rows = [
        "2026-11-22T22:00:00Z | CLAIM | lane=child-lane-1 | actor=x | model=y | ticket=OMN-1"
        " | est ~1 lane-hours; displaces z; (OMN-1) | parent=orchestrator-31d9-swarm | child",
        "2026-11-22T22:30:00Z | TERMINAL | lane=child-lane-1 | ticket=OMN-1 | outcome=done | ended",
        REFUSED_TERMINAL,
    ]
    ports = FakePorts(rows)
    result = _fire(ports)
    assert result.alarms[0].orchestrator is None
    assert _msgs(ports) == []
