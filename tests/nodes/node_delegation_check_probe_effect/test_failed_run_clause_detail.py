# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed gateway check names why in its run record, not only which clause failed (OMN-26043).

Measured on the dev lane: C29 and C30 failed ``completed_under_tenant_key`` / ``completed_through_gateway`` because the test
tenant had no provider key (the gateway row read ``failed`` with code ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT). The clause
detail read only ``the run reads 'failed' (submit answered 202)`` and the tick's evidence listed clause names with no detail,
so the cause was readable only in the gateway's database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)

CODE = "ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT"
REASON = "[ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT] no provider key is registered for this tenant"
SETTINGS = GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0)


def _failed_with(code: str, reason: str) -> Any:
    """Every run ends failed with the given gateway code and reason."""

    def override(m: str, p: str, a: str, b: Any) -> ModelGatewayObservation | None:
        return _failed_body(p, code, reason)

    return override


def _failed_body(p: str, code: str, reason: str) -> ModelGatewayObservation | None:
    if "/status" in p or "/receipt" in p:
        return ModelGatewayObservation(
            status=200,
            body={
                "workflow_id": "w-1",
                "status": "failed",
                "terminal_model_used": "none",
                "terminal_failure_class": None,
                "terminal_failure_code": code,
                "terminal_failure_reason": reason,
                "provider": None,
                "route": None,
            },
        )
    return None


def _gateway(code: str = CODE, reason: str = REASON) -> FakeGateway:
    gateway = FakeGateway()
    gateway.override = _failed_with(code, reason)
    return gateway


def _handler(gateway: FakeGateway) -> HandlerDelegationCheckProbe:
    clock = FakeClock()
    return HandlerDelegationCheckProbe(
        responders=(),
        reader=InMemoryResponderReader({}),
        machine=AbsentCustomerMachine("none"),
        publisher=InMemoryCheckPublisher(),
        topic="onex.evt.omnimarket.delegation-check-probe-completed.v1",
        clock=lambda: datetime(2026, 11, 26, 1, 0, tzinfo=UTC),
        gateway=gateway,
        gateway_settings=SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("check", "clause"),
    [
        ("C29", "completed_under_tenant_key"),
        ("C30", "completed_through_gateway"),
    ],
)
def test_a_failed_run_clause_names_the_failure_code_the_gateway_recorded(
    check: CheckId, clause: str
) -> None:
    clock = FakeClock()
    reading = run_gateway_check(
        check, _gateway(), SETTINGS, sleep=clock.sleep, monotonic=clock.monotonic
    )
    failed = {c.name: c for c in reading.clauses if not c.ok}
    assert clause in failed
    assert CODE in failed[clause].detail
    assert "no provider key is registered" in failed[clause].detail


@pytest.mark.unit
def test_the_tick_evidence_carries_the_detail_of_each_failed_clause(
    tmp_path: Path,
) -> None:
    other = "ONEX_MARKET_PROVIDER_UPSTREAM_ERROR"
    result = _handler(
        _gateway(other, f"[{other}] the provider answered an error")
    ).handle(ModelDelegationCheckRequest(correlation_id="c-detail", check_id="C29"))
    assert result.verdict is EnumCheckVerdict.FAIL
    evidence = tick.evidence_for(result)
    assert evidence["failed_clauses"] == [
        "completed_under_tenant_key",
        "receipt_names_provider_and_model",
    ]
    detail = evidence["failed_clause_details"]
    assert set(detail) == set(evidence["failed_clauses"])
    assert other in detail["completed_under_tenant_key"]
    # The record is JSON the unit writes to its state directory.
    (tmp_path / "e.json").write_text(json.dumps(evidence))
