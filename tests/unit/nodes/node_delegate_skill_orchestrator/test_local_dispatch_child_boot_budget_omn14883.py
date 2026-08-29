# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14883: the spawn child's interpreter boot is not the endpoint's timeout.

``_run_effect_handler_with_killable_timeout`` runs the blocking delegation effect
behind a child-process boundary (OMN-13597) which is a ``spawn`` child on macOS
(OMN-13842). A ``spawn`` child is a *fresh interpreter*: before it can call the
pickled effect handler it must re-import that handler's module and the whole
transitive package tree behind it. That import is pure infrastructure cost — it
happens before a single byte reaches the LLM endpoint.

The defect this suite pins: the parent started ONE deadline
(``timeout_seconds + _DISPATCH_TIMEOUT_BUFFER_SECONDS``, resolved from the
backend's contract-declared ``timeout_ms``) at ``process.start()``, so the
child's interpreter boot was charged against the endpoint's transport budget.
Measured import cost of the effect path is ~18s on macOS and ~80s on the .201
gate-runner (user CPU ~7.9s on both — the delta is I/O off the container mount)
against a budget as small as 10.5s. The parent then killed a perfectly healthy
child mid-import and projected a canonical ``TIMEOUT`` failure with
``endpoint_healthy=False`` — slandering an endpoint that was never contacted.

The fix separates the two phases the one deadline was conflating:

* BOOT — the child signals readiness once its imports are done. Bounded by a
  budget derived from a measured in-run calibration of the same environment's
  boot cost, never by the endpoint's transport timeout, and its expiry is an
  infrastructure error, never an endpoint verdict.
* CALL — the endpoint's contract-resolved budget, started only once the child is
  ready. Unchanged semantics: expiry here is still a real ``TimeoutError``.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _resolve_child_boot_budget_seconds,
    _run_effect_handler_with_killable_timeout,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)


class ImmediateEffectHandler:
    """Pickleable effect double that returns as soon as the child is booted.

    Its call costs no measurable time, so any wall-clock the parent observes is
    the child's interpreter boot and nothing else.
    """

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="booted",
            output_hash="omn14883-output-hash",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )


class SlowCallEffectHandler:
    """Pickleable effect double whose CALL (not boot) overruns the budget."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        time.sleep(30.0)
        raise AssertionError("unreachable: the parent must kill this child first")


def _call_request() -> ModelLlmDelegationCallRequest:
    correlation_id = str(uuid4())
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=correlation_id,
        causation_id=correlation_id,
        model_id="Qwen3.6-35B-A3B",
        endpoint_ref="http://unreachable.invalid:8000/v1/chat/completions",
        prompt="say hello",
        prompt_hash="",
        system_prompt="you are a test double",
        task_type="research",
        max_tokens=16,
        timeout_seconds=0.5,
        model_tier="local",
        provider="local-coder",
    )


def test_boot_budget_scales_off_a_measured_observation() -> None:
    """A measured boot observation drives the budget, not a bare constant."""
    observed = 40.0
    budget = _resolve_child_boot_budget_seconds(observed_boot_seconds=observed)
    assert budget >= observed * 2, (
        "a budget that does not clear the boot cost already measured in this "
        "environment cannot bound a boot in this environment"
    )
    assert budget <= port_module._EFFECT_CHILD_BOOT_CEILING_SECONDS


def test_boot_budget_floors_a_fast_observation() -> None:
    """A fast observation must not shrink the budget below the floor.

    Boot cost is I/O-bound and bursty; one fast sample must not arm a budget that
    the next cold-cache boot on the same host blows.
    """
    budget = _resolve_child_boot_budget_seconds(observed_boot_seconds=0.01)
    assert budget >= port_module._EFFECT_CHILD_BOOT_FLOOR_SECONDS


def test_uncalibrated_boot_budget_is_the_ceiling() -> None:
    """With no observation yet, the budget is the documented hang-guard ceiling."""
    budget = _resolve_child_boot_budget_seconds(observed_boot_seconds=None)
    assert budget == port_module._EFFECT_CHILD_BOOT_CEILING_SECONDS


def test_child_boot_is_not_charged_to_the_endpoint_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn child that boots slower than the endpoint budget still returns.

    ``timeout_seconds`` here is the endpoint's whole budget (0.5s) and the child
    is a real ``spawn`` child that must re-import this test module and the port
    module behind it — tens of seconds on both sanctioned hosts. Before the fix
    the parent killed it mid-import and raised ``TimeoutError``; the effect never
    ran. The typed result coming back is proof the boot was measured separately.
    """
    monkeypatch.setattr(port_module.sys, "platform", "darwin")

    result = asyncio.run(
        _run_effect_handler_with_killable_timeout(
            ImmediateEffectHandler(),
            _call_request(),
            timeout_seconds=0.5,
        )
    )

    assert isinstance(result, ModelLlmDelegationCallResult)
    assert result.content == "booted"


def test_call_overrun_after_boot_is_still_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint budget still bounds the CALL once the child is ready.

    Excluding boot from the endpoint budget must not weaken the bound OMN-13597
    added: a booted child that stalls on the wire is still killed on the
    contract-resolved deadline.
    """
    monkeypatch.setattr(port_module.sys, "platform", "darwin")

    async def _run() -> None:
        await _run_effect_handler_with_killable_timeout(
            SlowCallEffectHandler(),
            _call_request(),
            timeout_seconds=1.0,
        )

    with pytest.raises(TimeoutError):
        asyncio.run(_run())
