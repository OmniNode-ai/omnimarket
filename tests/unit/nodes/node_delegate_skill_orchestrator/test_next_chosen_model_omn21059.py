# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21059: the models a customer chose for a key are tried in the order they chose.

``onex models`` stores an ordered list of models per provider key. The local
route used to read only the first entry, so a retryable failure or a
quality-gate FAIL on that model ended on the next KEY (or the run) and the
customer's second and third picks on the same key were never reached.

The ladder is the shipped one, the key and its model list are stored the way
``onex models add`` and ``add-model`` store them, and only the provider call
is faked. No house credential is set anywhere, so a pass cannot come from one.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_backend_by_id
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit

FIRST = "nvidia/nemotron-3-super-120b-a12b:free"
SECOND = "google/gemma-4-31b-it:free"
THIRD = "meta-llama/llama-4-scout:free"
GEMINI_MODEL = "gemini-3.5-flash-lite"

_openrouter = resolve_byok_backend_by_id("byok-openrouter")
assert _openrouter is not None
OPENROUTER_URL = _openrouter.endpoint_url
_ANSWER = (
    "### ANSWER\n"
    "A hash map stores values under keys and finds each one by hashing its "
    "key to a bucket, so a lookup does not scan every entry."
)


class _Providers:
    """Answers per model: every model in ``failing`` is unreachable."""

    def __init__(self, failing: set[str], *, refused: set[str] | None = None) -> None:
        self.failing = failing
        self.refused = refused or set()
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.endpoint_ref, request.model_id))
        common = {
            "request_id": request.request_id,
            "latency_ms": 5,
            "actual_cost_usd": Decimal("0"),
            "savings_usd": Decimal("0"),
        }
        if request.model_id in self.failing:
            return ModelLlmDelegationCallResult(
                success=False,
                content="",
                error_message=f"endpoint {request.endpoint_ref} failed health probe",
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                **common,
            )
        content = "" if request.model_id in self.refused else _ANSWER
        return ModelLlmDelegationCallResult(
            success=True, content=content, tokens_in=11, tokens_out=22, **common
        )


def _add_key(provider: str, models: tuple[str, ...]) -> None:
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=f"llm.{provider}.api_key",
            value=SecretStr(f"test-{provider}-key-not-a-real-one"),
            models=models,
            force=True,
        )
    )


@pytest.fixture
def chosen_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "LLM_GLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    db_path = use_local_store(monkeypatch, tmp_path)
    _add_key("openrouter", (FIRST, SECOND, THIRD))
    return db_path


def _dispatch(
    tmp_path: Path, providers: _Providers, *, model: str | None = None
) -> dict[str, object]:
    port = LocalDelegationDispatchPort(
        effect_handler=providers,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    return asyncio.run(
        port.dispatch(
            prompt="In two sentences, explain what a hash map is.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="acme",
            model=model,
        )
    )


def _models(providers: _Providers) -> list[str]:
    return [model for _, model in providers.calls]


def test_a_healthy_first_model_answers_alone(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing=set())
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "completed", result
    assert _models(providers) == [FIRST]


def test_a_retryable_failure_on_the_first_model_moves_to_the_second(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing={FIRST})
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "completed", result
    assert _models(providers)[-1] == SECOND
    assert THIRD not in _models(providers)
    assert {endpoint for endpoint, _ in providers.calls} == {OPENROUTER_URL}


def test_the_models_are_tried_in_the_order_the_customer_set(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing={FIRST, SECOND})
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "completed", result
    tried = list(dict.fromkeys(_models(providers)))
    assert tried == [FIRST, SECOND, THIRD]


def test_a_quality_gate_fail_on_the_first_model_moves_to_the_second(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing=set(), refused={FIRST})
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "completed", result
    tried = list(dict.fromkeys(_models(providers)))
    assert tried == [FIRST, SECOND]


def test_every_chosen_model_failing_ends_the_run_once_each_is_tried(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing={FIRST, SECOND, THIRD})
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "failed", result
    assert set(_models(providers)) == {FIRST, SECOND, THIRD}


def test_a_model_the_caller_names_is_never_swapped_for_a_later_pick(
    chosen_models: Path, tmp_path: Path
) -> None:
    providers = _Providers(failing={SECOND})
    result = _dispatch(tmp_path, providers, model=SECOND)

    assert result["status"] == "failed", result
    assert set(_models(providers)) == {SECOND}
