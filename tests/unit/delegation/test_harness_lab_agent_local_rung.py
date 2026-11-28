# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-agent harness runs the local least-busy model through the local rung (OMN-19998)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_process_runner import (
    HandlerHarnessProcessRunner,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessRunReceipt,
)

pytestmark = pytest.mark.unit


class _Environment:
    def environment(self) -> dict[str, str]:
        return {"PATH": "/usr/bin"}


class _LocalRung:
    """Stands for the plain local rung: records what it was asked, answers as the local model."""

    def __init__(self, receipt: ModelHarnessRunReceipt) -> None:
        self.receipt = receipt
        self.calls: list[tuple[str, str, float]] = []

    async def dispatch(
        self, *, prompt: str, task_type: str, timeout_seconds: float
    ) -> ModelHarnessRunReceipt:
        self.calls.append((prompt, task_type, timeout_seconds))
        return self.receipt


def _backend(**extra: Any) -> ModelDelegationBackendConfig:
    return ModelDelegationBackendConfig(
        backend_id="harness-lab-agent",
        provider="lab",
        kind="harness",
        harness="lab-agent",
        surface="internal",
        tenant_scope="house",
        model_name="local-model",
        tier="harness",
        capabilities=["document"],
        **extra,
    )


async def test_lab_agent_runs_the_local_least_busy_rung() -> None:
    rung = _LocalRung(
        ModelHarnessRunReceipt(
            answer="the answer",
            tokens_in=7,
            tokens_out=3,
            cost_usd=Decimal("0"),
            wall_seconds=0.2,
            exit_cause="completed",
            model="Qwen3.8-27B",
        )
    )
    runner = HandlerHarnessProcessRunner(
        environment=_Environment(), agents={}, local_rung=rung
    )
    got = await runner.run(_backend(), "the prompt", 30)
    assert got.exit_cause == "completed"
    assert got.answer == "the answer"
    assert got.model == "Qwen3.8-27B"
    assert rung.calls == [("the prompt", "document", 30)]


async def test_lab_agent_without_a_local_rung_is_still_launch_failed() -> None:
    runner = HandlerHarnessProcessRunner(environment=_Environment(), agents={})
    got = await runner.run(_backend(), "p", 30)
    assert got.exit_cause == "launch_failed"


async def test_local_rung_pins_the_local_tier_pick_and_runs_the_local_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.handlers import lab_agent_local_rung as mod
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    picks: list[tuple[str, str]] = []

    def fake_pick(tier: str, task_type: str, **_k: Any) -> str:
        picks.append((tier, task_type))
        return "local-coder"

    monkeypatch.setattr(routing, "backend_id_for_tier", fake_pick)
    monkeypatch.setattr(mod, "read_spread_load_snapshot", lambda: None)

    class Port:
        kwargs: dict[str, Any] = {}

        async def dispatch(self, **kwargs: Any) -> dict[str, object]:
            Port.kwargs = kwargs
            return {
                "status": "completed",
                "content": "ok",
                "model_name": "m1",
                "input_tokens": 4,
                "output_tokens": 1,
            }

    got = await mod.HandlerLabAgentLocalRung(Port()).dispatch(
        prompt="p", task_type="document", timeout_seconds=30
    )
    assert picks == [("local", "document")]
    assert Port.kwargs["backend_id"] == "local-coder"
    assert "no_escalation" not in Port.kwargs
    assert (got.exit_cause, got.answer, got.model) == ("completed", "ok", "m1")
    assert (got.tokens_in, got.tokens_out) == (4, 1)
