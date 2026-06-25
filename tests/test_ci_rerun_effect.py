# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 7: Tests for node_ci_rerun_effect [OMN-8962]."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import SecretStr

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


@pytest.fixture(autouse=True)
def _mock_resolve_api_key_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out resolve_api_key_async for all ci_rerun tests (no GITHUB_TOKEN in CI)."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun.resolve_api_key_async",
        AsyncMock(return_value=SecretStr("fake-token")),
    )


def _cmd(run_id_github: str = "99887766") -> ModelCiRerunCommand:
    return ModelCiRerunCommand(
        pr_number=600,
        repo="OmniNode-ai/omni_home",
        run_id_github=run_id_github,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=3,
    )


@pytest.mark.asyncio
async def test_successful_rerun_returns_triggered_true() -> None:
    """GitHub rerun-failed-jobs API succeeds → rerun_triggered=True."""
    with patch.object(HandlerCiRerunEffect, "_rerun_sync", return_value=(True, None)):
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
    """GitHub rerun-failed-jobs API fails → rerun_triggered=False, error set."""
    with patch.object(
        HandlerCiRerunEffect,
        "_rerun_sync",
        return_value=(False, "run not found"),
    ):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.rerun_triggered is False
    assert evt.error == "run not found"


@pytest.mark.asyncio
async def test_elapsed_seconds_recorded() -> None:
    """Elapsed time is non-negative."""
    with patch.object(HandlerCiRerunEffect, "_rerun_sync", return_value=(True, None)):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_completion_event_carries_correct_metadata() -> None:
    """Completion event carries pr_number, repo, correlation_id, run_id, total_prs."""
    with patch.object(HandlerCiRerunEffect, "_rerun_sync", return_value=(True, None)):
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


# ---------------------------------------------------------------------------
# OMN-13416 — empty-commit re-trigger mode (CI event-delivery gap)
# ---------------------------------------------------------------------------


def _empty_commit_cmd() -> ModelCiRerunCommand:
    return ModelCiRerunCommand(
        pr_number=700,
        repo="OmniNode-ai/omni_home",
        run_id_github="",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=1,
        retrigger_mode="empty_commit",
        head_branch="feat/wedged",
        missing_required_contexts=("Runtime Sweep",),
    )


@pytest.mark.asyncio
async def test_empty_commit_mode_uses_empty_commit_path_not_rerun() -> None:
    """empty_commit mode must NOT call rerun-failed-jobs (no run to rerun)."""
    with (
        patch.object(
            HandlerCiRerunEffect,
            "_rerun_sync",
            side_effect=AssertionError("must not rerun in empty_commit mode"),
        ),
        patch.object(
            HandlerCiRerunEffect, "_empty_commit_sync", return_value=(True, None)
        ),
    ):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_empty_commit_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.rerun_triggered is True
    assert evt.error is None
    assert evt.pr_number == 700


@pytest.mark.asyncio
async def test_empty_commit_mode_failure_surfaces_error() -> None:
    with patch.object(
        HandlerCiRerunEffect,
        "_empty_commit_sync",
        return_value=(False, "push rejected"),
    ):
        handler = HandlerCiRerunEffect()
        output = await handler.handle(_empty_commit_cmd())

    evt = output.events[0]
    assert isinstance(evt, ModelCiRerunTriggeredEvent)
    assert evt.rerun_triggered is False
    assert evt.error == "push rejected"
