# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the external dead-man check compute."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_external_deadman_check_compute.handlers import (
    HandlerExternalDeadmanCheck,
)
from omnimarket.nodes.node_external_deadman_check_compute.models import (
    EnumExternalDeadmanAction,
    EnumExternalDeadmanReason,
    ModelExternalDeadmanCheckRequest,
    ModelExternalDeadmanIssue,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_external_deadman_check_compute"
)


def _ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _mirror(leg: str) -> str:
    return json.dumps(
        {
            "schema_version": "external-deadman-mirror/v1",
            "leg": leg,
            "written_at": _ago(minutes=3),
            "own_heartbeat_at": _ago(minutes=1),
            "peer_heartbeat_at": _ago(minutes=2),
            "undelivered": {"count": 0, "oldest_due_at": None},
            "unrecorded": {"count": 0, "oldest_due_at": None},
            "canary": {
                "injection_at": _ago(hours=3),
                "receipt_deadline": _ago(hours=2),
                "delivered": True,
                "recorded": True,
            },
        }
    )


def test_golden_chain_stale_run_opens_then_healthy_run_stays_quiet() -> None:
    """Both mirrors missing raises and opens one issue; fresh mirrors leave it alone."""
    handler = HandlerExternalDeadmanCheck()
    stale = handler.handle(
        ModelExternalDeadmanCheckRequest(
            now=NOW, primary_mirror=None, deadman_mirror=None
        )
    )
    assert stale.alarm
    assert {r.code for r in stale.reasons} == {
        EnumExternalDeadmanReason.BOTH_MIRRORS_STALE
    }
    assert stale.action is EnumExternalDeadmanAction.OPEN_ISSUE

    issue = ModelExternalDeadmanIssue(
        number=7, title=stale.issue_title, body=stale.issue_body
    )
    healthy = handler.handle(
        ModelExternalDeadmanCheckRequest(
            now=NOW,
            primary_mirror=_mirror("primary"),
            deadman_mirror=_mirror("deadman"),
            open_issues=(issue,),
        )
    )
    assert not healthy.alarm
    assert not healthy.notify


def test_error_chain_request_without_clock_is_refused() -> None:
    """A request with no clock is refused before the handler runs."""
    with pytest.raises(ValidationError):
        ModelExternalDeadmanCheckRequest.model_validate(
            {"primary_mirror": None, "deadman_mirror": None}
        )


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.external-deadman-check-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.external-deadman-check-completed.v1"
    ]
    assert contract["node_type"] == "compute"
    assert contract["handler"]["class"] == HandlerExternalDeadmanCheck.__name__
    assert HandlerExternalDeadmanCheck.__module__ == contract["handler"]["module"]
