# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A healthy delegation the runtime refuses because the test tenant holds no provider key is an absent precondition (OMN-26043).

The dev runtime refuses a customer tenant with no registered provider key (CustomerKeyRefusedError, code
ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT): no customer runs on a house key. A probe that submits a healthy delegation as such a
tenant reads that refusal from the terminal status. It names a missing instrument (the tenant's key), not a product defect, so the
reading is PRECONDITION-ABSENT with the refusal quoted, and every other clause is still recorded. Any other failed healthy run
still reads FAIL.
"""

from __future__ import annotations

from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelDelegationCheckReading,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)

SETTINGS = GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0)
KEY_ABSENT = "ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT"


def _refuse_healthy_runs(
    gateway: FakeGateway, *, code: str, failure_class: str
) -> None:
    """Every run that is not the dying one ends failed with ``code``, status and receipt in agreement."""
    base = gateway.override

    def refused(
        method: str, path: str, auth: str, body: Any
    ) -> ModelGatewayObservation | None:
        answer = gateway._answer(method, path, auth, body)
        is_read = method == "GET" and "/v1/workflows/" in path
        if not is_read or not isinstance(answer.body, dict):
            return base(method, path, auth, body)
        run = gateway._runs.get(path.split("/")[3])
        if run is None or run["dying"]:
            return None
        return ModelGatewayObservation(
            status=answer.status,
            body={
                **answer.body,
                "status": "failed",
                "terminal_model_used": "none",
                "terminal_failure_class": failure_class,
                "terminal_failure_code": code,
                "terminal_failure_reason": "no provider key is registered for this tenant",
                "rule_evaluations": [],
            },
        )

    gateway.override = refused


def _run(check: CheckId, gateway: FakeGateway) -> ModelDelegationCheckReading:
    clock = FakeClock()
    return run_gateway_check(
        check, gateway, SETTINGS, sleep=clock.sleep, monotonic=clock.monotonic
    )


@pytest.mark.unit
@pytest.mark.parametrize("check", ["C28", "C15", "C16", "C29"])
def test_a_tenant_with_no_provider_key_reads_precondition_absent_not_fail(
    check: CheckId,
) -> None:
    gateway = FakeGateway()
    _refuse_healthy_runs(
        gateway, code=KEY_ABSENT, failure_class="CustomerKeyRefusedError"
    )
    reading = _run(check, gateway)
    assert reading.verdict() is EnumCheckVerdict.PRECONDITION_ABSENT
    absent = [p for p in reading.preconditions if not p.present]
    assert [p.name for p in absent] == ["tenant_provider_key"]
    assert KEY_ABSENT in absent[0].detail


@pytest.mark.unit
def test_the_absent_key_hides_no_clause_the_run_still_graded() -> None:
    gateway = FakeGateway()
    _refuse_healthy_runs(
        gateway, code=KEY_ABSENT, failure_class="CustomerKeyRefusedError"
    )
    reading = _run("C28", gateway)
    by_name = {c.name: c.ok for c in reading.clauses}
    assert by_name["healthy_consumed"] is False
    assert "invalid_payload_observed" in by_name


@pytest.mark.unit
def test_a_healthy_run_that_fails_for_any_other_reason_still_reads_fail() -> None:
    gateway = FakeGateway()
    _refuse_healthy_runs(
        gateway, code="ONEX_ROUTING_NO_TIER", failure_class="RoutingError"
    )
    reading = _run("C28", gateway)
    assert reading.verdict() is EnumCheckVerdict.FAIL
    assert not reading.preconditions
