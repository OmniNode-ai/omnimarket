# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reasoning completes on the LOCAL tier without the cloud judge (OMN-13963).

WS-D/D2 dogfood found every `reasoning` `onex delegate` call hard-failing on a
GLM 429. The live capture (`node_delegate_skill_orchestrator-cf4e7b40-*.log`)
showed reasoning DID route to the local Qwen (`local-coder`, tier=local) first and
the local call ran — but the quality gate then ESCALATED with
``TASK_MISMATCH: no deterministic acceptance or judge adequacy authority;
schema/length/no-refusal/marker checks are reject-only`` and fell through to
`cheap_cloud` (GLM), which 429'd.

Root cause: `reasoning`'s DoD (`response_non_empty` / `no_refusal` /
`step_by_step_explanation`) was ENTIRELY reject-only, so `_has_adequacy_authority`
was False and a valid LOCAL answer could never PASS — the gate force-escalated to
the throttled cloud judge on every call. `reasoning` is NOT judge-combinable
(unlike code_generation), so no judge score ever backfilled the missing authority.

OMN-13963 adds `semantic_adequacy` (a real LOCAL completeness authority — the SAME
one the sibling reproducible_judge classes `document`/`research`/`summarization`
already carry) to `reasoning` + `complex_reasoning`. A valid local answer is now
accepted on the local tier; a refusal/empty still fails. No cloud judge needed.

These tests drive the REAL quality gate (`resolve_task_class_dod_checks` reads the
committed `task_class_contracts.v1.yaml`), so they fail if the DoD authority is
removed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

# A complete, non-refusing, step-by-step reasoning answer: passes response_non_empty,
# no_refusal, step_by_step_explanation (step/first/then markers) AND semantic_adequacy
# (a complete answer, not a truncated fragment).
_GOOD_REASONING = (
    "Binary search is O(log n) because it halves the search space each step. "
    "First, it compares the target to the middle element. Then, if the target is "
    "smaller it discards the upper half, otherwise the lower half. Each step halves "
    "n, so after k steps the space is n / 2^k; the search ends when n / 2^k = 1, "
    "i.e. k = log2(n) steps."
)

_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.6-35B-A3B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


def _no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the initial backend to local and make the ladder single-tier.

    Isolates the acceptance decision from routing: if the gate does NOT accept the
    local answer it would escalate (the bug); with no next tier available a
    non-accepted result terminates FAILED at the local tier. So a `completed`
    result with escalation_count 0 proves the gate accepted local output.
    """
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _LOCAL_BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 1
    )


def _effect_returning(content: str) -> port_mod._EffectHandler:
    def _effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=20,
            tokens_out=90,
            latency_ms=13000,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    return _effect


def _dispatch(
    port: LocalDelegationDispatchPort, *, task_type: str
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="Explain why binary search is O(log n).",
            task_type=task_type,
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )


@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_reasoning_completes_on_local_without_cloud_judge(
    task_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid reasoning answer is accepted on the LOCAL tier — no escalation.

    Pre-OMN-13963 the gate had no adequacy authority for reasoning, so this exact
    answer force-escalated (and, in prod, hit the throttled GLM 429). With
    `semantic_adequacy` in the DoD it is accepted on local.
    """
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(_GOOD_REASONING),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type=task_type)

    assert result["status"] == "completed", result
    assert result["quality_gate_passed"] is True
    assert result["escalation_count"] == 0  # accepted on local, never escalated
    assert result["model_name"] == "Qwen3.6-35B-A3B"


def test_reasoning_refusal_still_fails_on_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guardrail: the local authority accepts a real answer, not "anything".

    A refusal fails `no_refusal` and is not accepted — the fix adds a real
    completeness authority, it does not weaken the gate.
    """
    _no_escalation(monkeypatch)
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning("I cannot help with that request."),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="reasoning")

    assert result["status"] == "failed"
    assert result["quality_gate_passed"] is False


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_contract_declares_local_adequacy_authority(task_type: str) -> None:
    """The committed contract carries a non-reject-only heuristic authority.

    `semantic_adequacy` is the local acceptance authority (OMN-13963); without it
    the class has NO adequacy authority and force-escalates. This asserts the DoD
    fix is present in `task_class_contracts.v1.yaml`.
    """
    _det, heur = resolve_task_class_dod_checks(task_type)
    assert "semantic_adequacy" in heur, (
        f"{task_type} must declare a local adequacy authority (semantic_adequacy) "
        f"so valid local output is accepted without the cloud judge; got {heur}"
    )
