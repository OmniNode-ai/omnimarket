# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 7: Tests for node_ci_rerun_effect [OMN-8962]."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun import (
    HandlerCiRerunEffect,
)
from omnimarket.nodes.node_ci_rerun_effect.models.model_ci_rerun_triggered_event import (
    ModelCiRerunTriggeredEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelCiRerunCommand,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


def _cmd(run_id_github: str = "99887766") -> ModelCiRerunCommand:
    return ModelCiRerunCommand(
        pr_number=600,
        repo="OmniNode-ai/omni_home",
        run_id_github=run_id_github,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=3,
    )


def _mock_proc(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.asyncio
async def test_successful_rerun_returns_triggered_true() -> None:
    """gh run rerun --failed succeeds → rerun_triggered=True."""
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd())

    assert len(output.events) == 1
    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.rerun_triggered is True
    assert evt.error is None
    assert evt.run_id_github == "99887766"
    assert output.result is None


@pytest.mark.asyncio
async def test_failed_rerun_returns_triggered_false_with_error() -> None:
    """gh run rerun fails → rerun_triggered=False, error set."""
    mock_proc = _mock_proc(returncode=1, stderr=b"run not found")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.rerun_triggered is False
    assert evt.error == "run not found"


@pytest.mark.asyncio
async def test_elapsed_seconds_recorded() -> None:
    """Elapsed time is non-negative."""
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_completion_event_carries_correct_metadata() -> None:
    """Completion event carries pr_number, repo, correlation_id, run_id, total_prs."""
    mock_proc = _mock_proc(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd("12345678"))

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.pr_number == 600
    assert evt.repo == "OmniNode-ai/omni_home"
    assert evt.correlation_id == _CORR_ID
    assert evt.run_id == _RUN_ID
    assert evt.total_prs == 3
    assert evt.run_id_github == "12345678"
