# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 5: Tests for node_merge_sweep_auto_merge_arm_effect [OMN-8960]."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_armed_event import (
    ModelAutoMergeArmedEvent,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_unarmed_clean_alert_event import (
    ModelAutoMergeUnarmedCleanAlertEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


@pytest.fixture(autouse=True)
def _mock_resolve_api_key_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out resolve_api_key_async for all auto_merge_arm tests (no GITHUB_TOKEN in CI)."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm.resolve_api_key_async",
        AsyncMock(return_value=SecretStr("fake-token")),
    )


def _cmd(pr_node_id: str = "PR_kwXXXXXX") -> ModelAutoMergeArmCommand:
    return ModelAutoMergeArmCommand(
        pr_number=100,
        repo="OmniNode-ai/omni_home",
        pr_node_id=pr_node_id,
        head_ref_name="feat/test",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=3,
    )


@pytest.mark.asyncio
async def test_successful_arm_returns_for_effect_with_completion() -> None:
    """Happy path: GraphQL succeeds → armed=True completion event in output."""
    with patch.object(
        HandlerAutoMergeArmEffect, "_arm_sync", return_value=(True, None)
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    assert len(output.events) == 1
    evt = output.events[0]
    assert isinstance(evt, ModelAutoMergeArmedEvent)
    assert evt.armed is True
    assert evt.error is None
    assert evt.pr_number == 100
    assert evt.total_prs == 3
    # Orchestrator result must be None (effect never returns result)
    assert output.result is None


@pytest.mark.asyncio
async def test_failed_arm_returns_armed_false_with_error() -> None:
    """GraphQL fails → armed=False, error set."""
    with patch.object(
        HandlerAutoMergeArmEffect,
        "_arm_sync",
        return_value=(False, "auth error"),
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    # OMN-13322: a failed arm now also emits an unarmed-CLEAN alert event, so the
    # completion is the FIRST event (not the only one).
    evt = output.events[0]
    assert isinstance(evt, ModelAutoMergeArmedEvent)
    assert evt.armed is False
    assert evt.error == "auth error"


@pytest.mark.asyncio
async def test_elapsed_seconds_recorded() -> None:
    """Elapsed time is recorded in completion event (non-negative)."""
    with patch.object(
        HandlerAutoMergeArmEffect, "_arm_sync", return_value=(True, None)
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelAutoMergeArmedEvent)
    assert evt.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_idempotent_already_armed_succeeds() -> None:
    """Re-arming an already-armed PR: GitHub GraphQL returns success (idempotent)."""
    with patch.object(
        HandlerAutoMergeArmEffect, "_arm_sync", return_value=(True, None)
    ):
        handler = HandlerAutoMergeArmEffect()
        output1 = await handler.handle(_cmd())
        output2 = await handler.handle(_cmd())

    assert output1.events[0].armed is True
    assert output2.events[0].armed is True


# --- OMN-13322: arm-trigger path + unarmed-CLEAN alert path ---


@pytest.mark.asyncio
async def test_arm_trigger_clean_pr_emits_only_completion_no_alert() -> None:
    """Arm-trigger path: a CLEAN PR that arms successfully emits ONLY the
    completion event — no unarmed-CLEAN alert."""
    with patch.object(
        HandlerAutoMergeArmEffect, "_arm_sync", return_value=(True, None)
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    assert len(output.events) == 1
    assert isinstance(output.events[0], ModelAutoMergeArmedEvent)
    assert not any(
        isinstance(e, ModelAutoMergeUnarmedCleanAlertEvent) for e in output.events
    )


@pytest.mark.asyncio
async def test_unarmed_clean_pr_emits_alert_event() -> None:
    """Alert path: a CLEAN PR that fails to arm (e.g. missing OCC preflight,
    OMN-10485) emits the completion AND a distinct unarmed-CLEAN alert."""
    with patch.object(
        HandlerAutoMergeArmEffect,
        "_arm_sync",
        return_value=(False, "OCC preflight missing"),
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    assert len(output.events) == 2
    completion = output.events[0]
    alert = output.events[1]
    assert isinstance(completion, ModelAutoMergeArmedEvent)
    assert completion.armed is False
    assert isinstance(alert, ModelAutoMergeUnarmedCleanAlertEvent)
    assert alert.reason == "OCC preflight missing"
    assert alert.pr_number == 100
    assert alert.repo == "OmniNode-ai/omni_home"
    assert alert.total_prs == 3
    assert alert.correlation_id == _CORR_ID
    assert alert.run_id == _RUN_ID


@pytest.mark.asyncio
async def test_unarmed_clean_alert_reason_defaults_when_error_none() -> None:
    """If the arm fails with no error string, the alert still carries a
    non-empty reason (an alert without a reason is a contradiction)."""
    with patch.object(
        HandlerAutoMergeArmEffect, "_arm_sync", return_value=(False, None)
    ):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    alert = output.events[1]
    assert isinstance(alert, ModelAutoMergeUnarmedCleanAlertEvent)
    assert alert.reason  # non-empty
