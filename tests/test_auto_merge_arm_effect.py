# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 5: Tests for node_merge_sweep_auto_merge_arm_effect [OMN-8960]."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_armed_event import (
    ModelAutoMergeArmedEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


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


def _mock_proc(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b'{"data": {}}', stderr))
    return proc


@pytest.mark.asyncio
async def test_successful_arm_returns_for_effect_with_completion() -> None:
    """Happy path: GraphQL succeeds → armed=True completion event in output."""
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
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
    mock_proc = _mock_proc(returncode=1, stderr=b"auth error")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    assert len(output.events) == 1
    evt = output.events[0]
    assert isinstance(evt, ModelAutoMergeArmedEvent)
    assert evt.armed is False
    assert evt.error == "auth error"


@pytest.mark.asyncio
async def test_elapsed_seconds_recorded() -> None:
    """Elapsed time is recorded in completion event (non-negative)."""
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelAutoMergeArmedEvent)
    assert evt.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_idempotent_already_armed_succeeds() -> None:
    """Re-arming an already-armed PR: GitHub GraphQL returns success (idempotent)."""
    # GitHub returns exit 0 even when already armed — simulate that
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerAutoMergeArmEffect()
        output1 = await handler.handle(_cmd())
        output2 = await handler.handle(_cmd())

    assert output1.events[0].armed is True
    assert output2.events[0].armed is True
