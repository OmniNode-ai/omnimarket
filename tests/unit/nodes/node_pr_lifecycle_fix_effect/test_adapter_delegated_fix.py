# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DelegatedFixAdapter bridge tests (OMN-13940).

Proves the adapter raises on any non-ACCEPTED outcome (so
HandlerPrLifecycleFix's two-strike/escalation logic treats it exactly like
any other adapter failure) and returns a descriptive string only when the
delegated fix actually succeeded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixResult,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_delegated_fix import (
    DelegatedFixAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


class _FakeHandler:
    def __init__(self, result: ModelDelegatedFixResult) -> None:
        self._result = result
        self.commands: list[object] = []

    async def handle(self, command: object) -> ModelDelegatedFixResult:
        self.commands.append(command)
        return self._result


def _delegated_result(outcome: EnumDelegatedFixOutcome) -> ModelDelegatedFixResult:
    return ModelDelegatedFixResult(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=42,
        outcome=outcome,
        delegation_model="ruff-deterministic",
        detail="test detail",
        completed_at=datetime.now(tz=UTC),
    )


def _fix_command() -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        block_reason=EnumPrBlockReason.CODE_FAILURE,
        ticket_id="OMN-13940",
        changed_files=["src/foo.py"],
        diff_total_lines=5,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestDelegatedFixAdapter:
    async def test_accepted_returns_descriptive_string(self) -> None:
        handler = _FakeHandler(_delegated_result(EnumDelegatedFixOutcome.ACCEPTED))
        adapter = DelegatedFixAdapter(handler=handler)  # type: ignore[arg-type]

        action = await adapter.dispatch_delegated_fix(
            "OmniNode-ai/omnimarket", 42, "OMN-13940", _fix_command()
        )

        assert "accepted" in action
        assert len(handler.commands) == 1

    @pytest.mark.parametrize(
        "outcome",
        [
            EnumDelegatedFixOutcome.NO_CHANGES,
            EnumDelegatedFixOutcome.GATE_FAILED,
            EnumDelegatedFixOutcome.REFUSED_SIZE_GATE,
            EnumDelegatedFixOutcome.REFUSED_DENYLIST,
            EnumDelegatedFixOutcome.REFUSED_NOT_A_WORKTREE,
            EnumDelegatedFixOutcome.ERROR,
        ],
    )
    async def test_non_accepted_outcomes_raise(
        self, outcome: EnumDelegatedFixOutcome
    ) -> None:
        handler = _FakeHandler(_delegated_result(outcome))
        adapter = DelegatedFixAdapter(handler=handler)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match=str(outcome)):
            await adapter.dispatch_delegated_fix(
                "OmniNode-ai/omnimarket", 42, "OMN-13940", _fix_command()
            )
