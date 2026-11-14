# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm owner responder reads the rows the lab alarm writer renders (OMN-24200)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_alarm_owner_responder_compute import (
    HandlerAlarmOwnerResponder,
    ModelAlarmOwnerPolicy,
    ModelAlarmOwnerRequest,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    render_ledger_row,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelLedgerSettings

pytestmark = pytest.mark.unit

CONDITION = "delegation_chain_canary"
SUBJECT = "OmniNode-ai/omnibase_infra chain-canary.yml@dev"


def test_a_rendered_lab_alarm_row_dispatches_on_its_cause_key() -> None:
    settings = ModelLedgerSettings(
        lane="lab-alarm",
        actor="launchd:lab-alarm",
        friction_class="lab-alarm",
        ticket="OMN-20769",
        cost="~0 lane-minutes at raise",
        evidence_limit=200,
        bus_lane="dev",
        workspace_root="registry-root",
        request_namespace="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        timeout_seconds=30.0,
        dedupe_window_minutes=60,
    )
    row = render_ledger_row(
        condition=CONDITION,
        subject=SUBJECT,
        detail="recorded",
        evidence="recorded",
        stamp=datetime(2026, 11, 3, 12, 52, 8, tzinfo=UTC),
        raised_at="2026-11-03T12:52:08Z",
        request_id="r",
        settings=settings,
    )
    policy = ModelAlarmOwnerPolicy(
        alarm_actor=settings.actor,
        conditions=(CONDITION,),
        owner_required_after_hours=6.0,
        redispatch_after_hours=24.0,
    )
    request = ModelAlarmOwnerRequest(
        rows=(row,), now="2026-11-13T12:00:00Z", policy=policy
    )
    result = HandlerAlarmOwnerResponder().handle(request)
    assert len(result.dispatches) == 1
    assert result.dispatches[0].cause_key == f"delegation-chain-canary:{SUBJECT}"
