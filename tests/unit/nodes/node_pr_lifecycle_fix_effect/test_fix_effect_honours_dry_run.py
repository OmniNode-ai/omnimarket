# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A dry_run fix command calls no adapter and posts no marker.

The PR landing orchestrator runs in shadow mode (github_mode dry_run) and
stamps dry_run on every companion command it sends. The fix effect logged the
flag and then routed the command to the live autobind adapter anyway, so a
shadow derive could push a companion and post a check-run marker. Every route
must reach no injected adapter when the command carries dry_run, and the
outcome reporter must not be called.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.pr_landing_companion import EnumPrLandingCompanionOp
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

_REPO = "OmniNode-ai/omnimarket"


class _Exploding:
    """Every adapter method raises: reaching one is the defect."""

    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name: str) -> Any:
        async def _call(*args: Any, **kwargs: Any) -> str:
            self.calls.append(name)
            raise AssertionError(f"dry_run command reached live adapter {name}")

        return _call


def _head(_repo: str, _pr: int, _token: str | None) -> str:
    return "a" * 40


def _command(reason: EnumPrBlockReason) -> ModelPrLifecycleFixCommand:
    autobind = reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=3420,
        repo=_REPO,
        block_reason=reason,
        ticket_id="OMN-17427",
        op=EnumPrLandingCompanionOp.DERIVE,
        command_id="landing-cmd-1" if autobind else None,
        dry_run=True,
        requested_at=datetime(2026, 10, 5, 13, 0, tzinfo=UTC),
    )


@pytest.mark.unit
@pytest.mark.parametrize("reason", list(EnumPrBlockReason))
async def test_dry_run_reaches_no_adapter(reason: EnumPrBlockReason) -> None:
    live = _Exploding()
    reported: list[str] = []
    handler = HandlerPrLifecycleFix(
        github_adapter=live,
        agent_dispatch_adapter=live,
        occ_contract_adapter=live,
        occ_autobind_adapter=live,
        delegation_fix_adapter=live,
        outcome_token_resolver=lambda: reported.append("token") or "t",
        head_sha_resolver=_head,
    )

    result = await handler.handle(_command(reason))

    assert live.calls == []
    assert reported == []
    assert result.fix_action is None or "live adapter" not in result.fix_action


@pytest.mark.unit
async def test_dry_run_autobind_still_answers_the_landing_workflow() -> None:
    live = _Exploding()
    reported: list[str] = []
    handler = HandlerPrLifecycleFix(
        occ_autobind_adapter=live,
        outcome_token_resolver=lambda: reported.append("token") or "t",
        head_sha_resolver=_head,
    )

    _result, outcome = await handler.handle_with_companion_outcome(
        _command(EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND)
    )

    assert live.calls == []
    assert outcome is not None
    assert outcome.head_sha == "a" * 40
