# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile proofs for ``backend-pinned-single-attempt.v1`` local dispatch."""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Never, cast
from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.wire.model_dispatch_policy import (
    DispatchPolicy,
    canonical_execution_binding_type,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

_POLICY = "backend-pinned-single-attempt.v1"
_RENDERED_CONTRACT_SHA256 = "a" * 64
_DEFAULT_RESPONSE_CONTRACT = object()
_GOOD_RESEARCH = '{"answer":"confirmed constrained result"}'
_GOOD_CODE = '{"artifact":"assert True"}'
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."

_requires_core_execution_binding = pytest.mark.skipif(
    canonical_execution_binding_type() is None,
    reason="requires Core ModelDelegationExecutionBinding",
)


def _backend() -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="local-coder-mlx",
        model_id="Qwen3.6-35B-A3B",
        endpoint_ref="https://local.example/v1/chat/completions",
        tier="local",
        max_tokens=4096,
        timeout_ms=30000,
    )


class _RecordingEffect:
    def __init__(
        self,
        result_kind: str,
        *,
        content: str = _GOOD_RESEARCH,
        served_model_id: str | None = "Qwen3.6-35B-A3B",
    ) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []
        self._result_kind = result_kind
        self._content = content
        self._served_model_id = served_model_id

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        if self._result_kind == "timeout":
            raise TimeoutError("ambiguous provider timeout")
        if self._result_kind == "transport":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class="model_unavailable",
                error_message="pinned adapter unavailable",
                actual_cost_usd=Decimal("0"),
                savings_usd=Decimal("0"),
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self._content if self._result_kind == "success" else _REFUSAL,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
            served_model_id=self._served_model_id,
        )


def _forbidden_routing(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("single-attempt policy consulted retry or fallback routing")


class _ForbiddenJudge:
    """Injected model-quality boundary that proves policy execution never calls it."""

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, **_kwargs: object) -> Never:
        self.calls += 1
        raise AssertionError("single-attempt policy invoked a model quality judge")


def _install_pinned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str | None]]:
    resolve_calls: list[tuple[str, str | None]] = []

    def _resolve(
        task_type: str, *, backend_id: str | None = None, **_kwargs: object
    ) -> ModelResolvedDelegationBackend:
        resolve_calls.append((task_type, backend_id))
        if backend_id != "local-coder-mlx":
            raise AssertionError(f"unexpected alternate backend {backend_id!r}")
        return _backend()

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", _resolve)
    monkeypatch.setattr(port_mod, "first_eligible_tier", _forbidden_routing)
    monkeypatch.setattr(port_mod, "next_eligible_tier", _forbidden_routing)
    monkeypatch.setattr(port_mod, "tier_max_retries", _forbidden_routing)
    monkeypatch.setattr(port_mod, "is_free_tier", _forbidden_routing)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")

    def _settings_must_not_be_read() -> object:
        raise AssertionError("single-attempt dispatch consulted settings")

    monkeypatch.setattr(port_mod, "get_settings", _settings_must_not_be_read)
    return resolve_calls


def _dispatch(
    port: LocalDelegationDispatchPort,
    correlation_id: UUID,
    *,
    task_type: str = "research",
    response_contract: dict[str, object] | None | object = _DEFAULT_RESPONSE_CONTRACT,
    dispatch_policy: str | None = _POLICY,
    rendered_contract_sha256: str | None = _RENDERED_CONTRACT_SHA256,
    tenant_id: str = "rsd-lab",
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="Explain the constrained lab result.",
            task_type=task_type,
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=tenant_id,
            backend_id="local-coder-mlx",
            dispatch_policy=cast(DispatchPolicy, dispatch_policy),
            rendered_contract_sha256=rendered_contract_sha256,
            response_contract=(
                {"type": "object", "minProperties": 1}
                if response_contract is _DEFAULT_RESPONSE_CONTRACT
                else cast(dict[str, object] | None, response_contract)
            ),
        )
    )


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_invokes_only_the_requested_backend_once_and_attests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve_calls = _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect("success")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    correlation_id = uuid4()

    result = _dispatch(port, correlation_id)

    assert resolve_calls == [("research", "local-coder-mlx")]
    assert len(effect.calls) == 1
    assert effect.calls[0].provider == "local-coder-mlx"
    assert result["status"] == "completed"
    assert result["attempts_count"] == 1
    assert result["escalation_count"] == 0
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    assert attempts[0]["tier"] == "local"
    assert attempts[0]["backend_id"] == "local-coder-mlx"
    assert attempts[0]["model_id"] == "Qwen3.6-35B-A3B"
    assert attempts[0]["quality_gate_passed"] is True
    assert attempts[0]["failure_class"] is None
    assert attempts[0]["error_message"] == ""
    assert result["execution_binding"] == {
        "correlation_id": str(correlation_id),
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "served_model_id": "Qwen3.6-35B-A3B",
        "rendered_contract_sha256": _RENDERED_CONTRACT_SHA256,
        "attempt_count": 1,
        "fallback_used": False,
        "judge_used": False,
        "dispatch_policy": _POLICY,
        "terminal_kind": "completed",
    }


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_terminalizes_quality_failure_without_retry_or_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect("quality")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, uuid4())

    assert len(effect.calls) == 1
    assert result["status"] == "failed"
    assert result["attempts_count"] == 1
    assert result["escalation_count"] == 0
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    attempt = attempts[0]
    assert attempt["failure_class"] == "quality_gate_failed"
    assert result["execution_binding"]["terminal_kind"] == "failed"
    assert result["execution_binding"]["fallback_used"] is False


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_terminalizes_transport_failure_without_alternate_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve_calls = _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect("transport")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, uuid4())

    assert resolve_calls == [("research", "local-coder-mlx")]
    assert len(effect.calls) == 1
    assert result["status"] == "failed"
    assert result["attempts_count"] == 1
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["failure_class"] == "unknown"
    assert "execution_binding" not in result


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_attributes_the_effect_confirmed_served_model_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint-confirmed model identity wins over the configured fallback."""
    _install_pinned_backend(monkeypatch)
    db_path = tmp_path / "delegation.sqlite"
    effect = _RecordingEffect("success", served_model_id="Qwen3.6-35B-A3B-served")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    correlation_id = uuid4()

    result = _dispatch(port, correlation_id, tenant_id="omninode")

    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["model_id"] == "Qwen3.6-35B-A3B-served"
    assert result["model_name"] == "Qwen3.6-35B-A3B-served"
    assert result["execution_binding"]["served_model_id"] == "Qwen3.6-35B-A3B-served"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT model_name FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("Qwen3.6-35B-A3B-served",)


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_missing_served_model_id_is_unbound_failed_and_not_projected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pinned_backend(monkeypatch)
    db_path = tmp_path / "delegation.sqlite"
    effect = _RecordingEffect("success", served_model_id=None)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )

    result = _dispatch(port, uuid4(), tenant_id="omninode")

    assert result["status"] == "failed"
    assert result["error_message"] == "pinned_served_model_id_required"
    assert "execution_binding" not in result
    assert result["attempts"][0]["model_id"] == ""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'delegation_events'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_requires_explicit_response_contract_without_judge_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect("success")
    judge = _ForbiddenJudge()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
        judge=cast(HandlerJudgeAdequacy, judge),
    )

    result = _dispatch(port, uuid4(), response_contract=None)

    assert result["status"] == "failed"
    assert result["quality_gates_failed"] == ["pinned_response_contract_required"]
    assert judge.calls == 0


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_effect_forbids_legacy_api_key_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pinned_backend(monkeypatch)
    backend = _backend().model_copy(
        update={"secret_ref": "llm.test.api_key", "api_key_env": "SHOULD_NOT_READ"}
    )
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_args, **_kwargs: backend
    )
    effect = _RecordingEffect("success")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    _dispatch(port, uuid4())

    assert effect.calls[0].secret_ref == "llm.test.api_key"
    assert effect.calls[0].api_key_env is None
    assert effect.calls[0].require_canonical_secret_ref is True


@pytest.mark.unit
@_requires_core_execution_binding
def test_policy_timeout_is_one_terminal_outcome_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve_calls = _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect("timeout")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    result = _dispatch(port, uuid4())

    assert result["status"] == "failed"
    assert "did not return within" in result["error_message"]
    assert result["attempts_count"] == 1
    assert len(effect.calls) == 1
    assert resolve_calls == [("research", "local-coder-mlx")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_type",
    ["code_generation", "test", "validator_generation", "refactor"],
)
@pytest.mark.parametrize(
    ("result_kind", "expected_status"),
    [("success", "completed"), ("quality", "failed"), ("transport", "failed")],
)
@_requires_core_execution_binding
def test_policy_has_one_inference_effect_and_never_calls_model_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    result_kind: str,
    expected_status: str,
) -> None:
    """All policy terminals are one adapter call with no judge/cloud/fallback call."""
    resolve_calls = _install_pinned_backend(monkeypatch)
    effect = _RecordingEffect(result_kind, content=_GOOD_CODE)
    judge = _ForbiddenJudge()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
        judge=cast(HandlerJudgeAdequacy, judge),
    )

    result = _dispatch(
        port,
        uuid4(),
        task_type=task_type,
    )

    assert result["status"] == expected_status
    assert len(effect.calls) == 1
    assert effect.calls[0].provider == "local-coder-mlx"
    assert judge.calls == 0
    assert resolve_calls == [(task_type, "local-coder-mlx")]
    assert result["attempts_count"] == 1
    assert result["escalation_count"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dispatch_policy", "rendered_contract_sha256", "message"),
    [
        ("unexpected-policy.v1", _RENDERED_CONTRACT_SHA256, "unknown dispatch_policy"),
        (None, _RENDERED_CONTRACT_SHA256, "requires an explicit dispatch_policy"),
        (_POLICY, None, "rendered_contract_sha256"),
        (_POLICY, "A" * 64, "lowercase SHA-256"),
    ],
)
def test_direct_port_rejects_unknown_or_unbound_policy_before_inference(
    tmp_path: Path,
    dispatch_policy: str | None,
    rendered_contract_sha256: str | None,
    message: str,
) -> None:
    effect = _RecordingEffect("success")
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    with pytest.raises(ValueError, match=message):
        _dispatch(
            port,
            uuid4(),
            dispatch_policy=dispatch_policy,
            rendered_contract_sha256=rendered_contract_sha256,
        )

    assert effect.calls == []
