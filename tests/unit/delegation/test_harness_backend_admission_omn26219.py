# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""harness_backend_admission: one test per refusal of a harness rung (OMN-26219)."""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from tests.unit.delegation.harness_support_omn26219 import (
    AcceptAllGate,
    ListCostSink,
    ScriptedRunner,
    call_request,
    load_overlay,
    receipt,
)

pytestmark = pytest.mark.unit

ANSWER = "def add(a, b):\n    return a + b\n"


def handler(runner: ScriptedRunner) -> HandlerHarnessBackendCall:
    return HandlerHarnessBackendCall(
        runner=runner,
        gate=AcceptAllGate(),
        cost_sink=ListCostSink(),
        overlay=load_overlay(),
    )


async def test_harness_backend_admission_house_internal_runs_the_first_glm_rung() -> (
    None
):
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(call_request("code_generation"))
    assert result.status == "completed"
    assert runner.ran == ["harness-claude-glm-flash"]


async def test_harness_backend_admission_refuses_a_non_house_tenant() -> None:
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(call_request(tenant_slug="a-customer"))
    assert result.status == "refused"
    assert runner.ran == []
    assert result.refused_rungs
    assert all(
        EnumHarnessRungRefusal.NOT_HOUSE_TENANT in rung.refusals
        for rung in result.refused_rungs
    )


async def test_harness_backend_admission_refuses_a_missing_tenant() -> None:
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(call_request(tenant_slug=None))
    assert result.status == "refused"
    assert runner.ran == []


async def test_harness_backend_admission_refuses_a_customer_surface() -> None:
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(
        call_request(surface=EnumDelegationBackendSurface.ANY)
    )
    assert result.status == "refused"
    assert runner.ran == []
    assert all(
        EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE in rung.refusals
        for rung in result.refused_rungs
    )


async def test_harness_backend_admission_refuses_a_pin_only_rung_outside_its_pin() -> (
    None
):
    """code_review lists the pin-only codex rung first: an unpinned request skips it and starts on glm."""
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(call_request("code_review"))
    assert result.status == "completed"
    assert runner.ran == ["harness-claude-glm-flash"]
    skipped = [r for r in result.refused_rungs if r.backend_id == "harness-codex"]
    assert [r.refusals for r in skipped] == [(EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,)]


async def test_harness_backend_admission_a_pin_runs_only_the_pinned_backend() -> None:
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(
        call_request("code_review", pinned_backend_id="harness-codex")
    )
    assert result.status == "completed"
    assert runner.ran == ["harness-codex"]


async def test_harness_backend_admission_a_pin_is_not_a_way_past_the_tenant_check() -> (
    None
):
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(
        call_request(
            "code_review", pinned_backend_id="harness-codex", tenant_slug="a-customer"
        )
    )
    assert result.status == "refused"
    assert runner.ran == []
    assert result.refused_rungs[0].refusals == (
        EnumHarnessRungRefusal.NOT_HOUSE_TENANT,
    )


async def test_harness_backend_admission_refuses_a_pin_for_a_class_the_backend_does_not_declare() -> (
    None
):
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(
        call_request("document", pinned_backend_id="harness-codex")
    )
    assert result.status == "refused"
    assert runner.ran == []
    assert "does not declare task class" in result.detail


async def test_harness_backend_admission_refuses_a_pin_naming_no_declared_backend() -> (
    None
):
    runner = ScriptedRunner([receipt(ANSWER)])
    result = await handler(runner).handle(
        call_request(pinned_backend_id="harness-not-declared")
    )
    assert result.status == "refused"
    assert runner.ran == []
