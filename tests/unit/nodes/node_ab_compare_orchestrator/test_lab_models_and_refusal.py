# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shipped registry resolves the lab models, and an undeclared model is refused.

The first group reproduces the dev runtime's environment: the lab endpoint URL
variables are set and no ``*_MODEL_NAME`` variable is. Every registry model was
then skipped with "no model id declared" and the completed event came back
PARTIAL with an empty comparison. The second group pins the refusal: a requested
model the registry does not declare is reported as refused and is never sent an
inference request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
    HandlerAbCompareOrchestrator,
    _load_registry,
    _resolve_models,
)
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
    ModelAbCompareStart,
)
from tests.unit.nodes.node_ab_compare_orchestrator.test_handler_ab_compare_orchestrator import (
    _REGISTRY_TWO_LOCAL,
    FakeEffectHandler,
    _make_handler_with_registry,
    _write_registry,
)

_LAB_A_URL = "http://lab-a.example.invalid:8000"
_LAB_B_URL = "http://lab-b.example.invalid:8000"
_LAB_MODEL_IDS = ("qwen3-coder-30b", "qwen3.8-27b-h202")

_MODEL_NAME_ENVS = (
    "LLM_CODER_MODEL_NAME",
    "LLM_LAB_H202_MODEL_NAME",
)


class _RecordingEffectHandler(FakeEffectHandler):
    """FakeEffectHandler that records the model of every inference request."""

    def __init__(self, error_for: set[str] | None = None) -> None:
        super().__init__(error_for=error_for)
        self.requests: list[Any] = []

    @property
    def requested_models(self) -> list[str]:
        return [request.model for request in self.requests]

    async def handle(self, request: Any) -> Any:
        self.requests.append(request)
        return await super().handle(request)


def _dev_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint URLs present, model-name variables absent: the dev runtime."""
    monkeypatch.setenv("LLM_CODER_URL", _LAB_A_URL)
    monkeypatch.setenv("LLM_LAB_H202_URL", _LAB_B_URL)
    for name in _MODEL_NAME_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_shipped_registry_resolves_each_lab_model_from_endpoint_env_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dev_runtime_env(monkeypatch)

    resolved, skipped = _resolve_models(_load_registry(), list(_LAB_MODEL_IDS))

    assert skipped == []
    assert {m.model_id for m in resolved} == set(_LAB_MODEL_IDS)
    assert all(m.model_id_resolved for m in resolved)
    assert {m.endpoint_url for m in resolved} == {_LAB_A_URL, _LAB_B_URL}


@pytest.mark.unit
def test_shipped_lab_models_are_priced_from_the_registry() -> None:
    by_id = {entry["id"]: entry for entry in _load_registry()}

    for model_id in _LAB_MODEL_IDS:
        entry = by_id[model_id]
        assert entry["location"] == "local"
        assert entry["cost_per_1k_input"] == 0.0
        assert entry["cost_per_1k_output"] == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_lab_models_both_run_and_both_carry_a_registry_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dev_runtime_env(monkeypatch)
    effect = _RecordingEffectHandler()
    handler = HandlerAbCompareOrchestrator(effect_handler=effect)

    result = await handler.handle(
        ModelAbCompareStart(
            task="Reply with exactly: OK",
            models=list(_LAB_MODEL_IDS),
            correlation_id="corr-two-lab",
        )
    )

    assert result.status == "COMPLETED"
    assert result.models_skipped == []
    assert result.models_refused == []
    assert {row.model_key for row in result.comparison} == set(_LAB_MODEL_IDS)
    assert len(effect.requested_models) == 2
    assert all(row.error == "" and row.cost_usd == 0.0 for row in result.comparison)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undeclared_model_is_refused_and_never_sent_an_inference_request() -> (
    None
):
    effect = _RecordingEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, effect)

    result = await handler.handle(
        ModelAbCompareStart(
            task="test", models=["not-in-the-registry"], correlation_id="corr-refuse"
        )
    )

    assert result.status == "REFUSED"
    assert result.models_refused == ["not-in-the-registry"]
    assert result.comparison == []
    assert effect.requested_models == []
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_declared_model_still_runs_when_another_requested_model_is_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)
    effect = _RecordingEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, effect)

    result = await handler.handle(
        ModelAbCompareStart(
            task="test",
            models=["stub-local-a", "not-in-the-registry"],
            correlation_id="corr-mixed",
        )
    )

    assert result.status == "PARTIAL"
    assert result.models_refused == ["not-in-the-registry"]
    assert [row.model_key for row in result.comparison] == ["stub-local-a"]
    assert effect.requested_models == ["stub-model-a"]
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_is_never_reported_as_a_refused_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)
    effect = _RecordingEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, effect)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-all")
    )

    assert result.models_refused == []
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inference_request_carries_the_complete_endpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The infra effect posts ``endpoint_url`` verbatim and refuses a request without it."""
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )

    _dev_runtime_env(monkeypatch)
    effect = _RecordingEffectHandler()
    handler = HandlerAbCompareOrchestrator(effect_handler=effect)

    await handler.handle(
        ModelAbCompareStart(
            task="Reply with exactly: OK",
            models=["qwen3-coder-30b"],
            correlation_id="corr-url",
        )
    )

    (request,) = effect.requests
    assert request.base_url == _LAB_A_URL
    assert HandlerLlmOpenaiCompatible._build_url(request) == (
        f"{_LAB_A_URL}/v1/chat/completions"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_endpoint_env_that_already_names_the_chat_path_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dev_runtime_env(monkeypatch)
    monkeypatch.setenv("LLM_CODER_URL", f"{_LAB_A_URL}/v1/chat/completions")
    effect = _RecordingEffectHandler()
    handler = HandlerAbCompareOrchestrator(effect_handler=effect)

    await handler.handle(
        ModelAbCompareStart(
            task="test", models=["qwen3-coder-30b"], correlation_id="corr-path"
        )
    )

    (request,) = effect.requests
    assert request.endpoint_url == f"{_LAB_A_URL}/v1/chat/completions"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_is_failed_when_every_model_errored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)
    effect = _RecordingEffectHandler(error_for={"stub-model-a"})
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, effect)

    result = await handler.handle(
        ModelAbCompareStart(
            task="test", models=["stub-local-a"], correlation_id="corr-all-error"
        )
    )

    assert result.status == "FAILED"
    assert result.comparison[0].error
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_is_partial_when_one_of_two_models_errored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")
    effect = _RecordingEffectHandler(error_for={"stub-model-a"})
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, effect)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-one-error")
    )

    assert result.status == "PARTIAL"
    assert sorted(row.error == "" for row in result.comparison) == [False, True]
    Path(reg_path).unlink(missing_ok=True)
