# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: a customer with two keys reaches the second when the first fails.

Measured on a fresh customer profile with a Gemini and an OpenRouter key, both
registered through ``onex models add``: an unpinned ``document`` delegation
lands on OpenRouter (``cheap_frontier``). When OpenRouter failed retryably the
route was retried on itself and the run then ended FAILED, and a quality-gate
FAIL ended it the same way, while the customer's Gemini key sat untried. The
only platform rungs that carry Gemini are ``explicit_pin_only``, so neither the
same-tier sibling nor the next tier ever offered it; the "any registered key"
substitution ran for the initial backend only.

The ladder is the shipped one, the keys are stored the way ``onex models add``
stores them, and only the provider call is faked. No house credential is set
anywhere, so a pass cannot come from one.
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
from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    resolve_byok_backend_by_id,
)
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit

OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
GEMINI_MODEL = "gemini-3.5-flash-lite"


def _row(backend_id: str) -> ModelByokProviderBackend:
    row = resolve_byok_backend_by_id(backend_id)
    assert row is not None, backend_id
    return row


OPENROUTER_URL = _row("byok-openrouter").endpoint_url
GEMINI_URL = _row("byok-gemini").endpoint_url
_ANSWER = (
    "### ANSWER\n"
    "A hash map stores values under keys and finds each one by hashing its "
    "key to a bucket, so a lookup does not scan every entry."
)


class _Providers:
    """Answers per endpoint: OpenRouter as told, Gemini always with an answer."""

    def __init__(self, openrouter: str) -> None:
        self.openrouter = openrouter
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.endpoint_ref)
        common = {
            "request_id": request.request_id,
            "latency_ms": 5,
            "actual_cost_usd": Decimal("0"),
            "savings_usd": Decimal("0"),
        }
        if request.endpoint_ref == OPENROUTER_URL and self.openrouter == "unreachable":
            return ModelLlmDelegationCallResult(
                success=False,
                content="",
                error_message=f"endpoint {OPENROUTER_URL} failed health probe",
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                **common,
            )
        if request.endpoint_ref == OPENROUTER_URL and self.openrouter == "unauthorized":
            return ModelLlmDelegationCallResult(
                success=False,
                content="",
                error_message="provider refused the key (401)",
                failure_class=EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
                **common,
            )
        content = (
            ""
            if request.endpoint_ref == OPENROUTER_URL and self.openrouter == "empty"
            else _ANSWER
        )
        return ModelLlmDelegationCallResult(
            success=True, content=content, tokens_in=11, tokens_out=22, **common
        )


def _add_key(provider: str, model: str) -> None:
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=f"llm.{provider}.api_key",
            value=SecretStr(f"test-{provider}-key-not-a-real-one"),
            model=model,
            force=True,
        )
    )


@pytest.fixture
def two_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "LLM_GLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    db_path = use_local_store(monkeypatch, tmp_path)
    _add_key("gemini", GEMINI_MODEL)
    _add_key("openrouter", OPENROUTER_MODEL)
    return db_path


def _dispatch(
    tmp_path: Path, providers: _Providers, *, task_type: str = "document"
) -> dict[str, object]:
    port = LocalDelegationDispatchPort(
        effect_handler=providers,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    return asyncio.run(
        port.dispatch(
            prompt="In two sentences, explain what a hash map is.",
            task_type=task_type,
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
        )
    )


def _backends(result: dict[str, object]) -> list[object]:
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    return [attempt["backend_id"] for attempt in attempts]


def test_with_both_keys_healthy_the_first_key_on_the_ladder_answers(
    two_keys: Path, tmp_path: Path
) -> None:
    providers = _Providers(openrouter="healthy")
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "completed", result
    assert _backends(result) == ["byok-openrouter"]
    assert providers.calls == [OPENROUTER_URL]


def test_a_retryable_failure_on_the_first_key_falls_through_to_the_second(
    two_keys: Path, tmp_path: Path
) -> None:
    providers = _Providers(openrouter="unreachable")
    result = _dispatch(tmp_path, providers)

    budget = _row("byok-openrouter").max_retries
    assert _backends(result) == ["byok-openrouter"] * (budget + 1) + ["byok-gemini"]
    assert result["status"] == "completed", result
    assert providers.calls[-1] == GEMINI_URL


def test_a_quality_gate_fail_on_the_first_key_falls_through_to_the_second(
    two_keys: Path, tmp_path: Path
) -> None:
    providers = _Providers(openrouter="empty")
    result = _dispatch(tmp_path, providers)

    backends = _backends(result)
    assert backends[0] == "byok-openrouter"
    assert backends[-1] == "byok-gemini"
    assert result["status"] == "completed", result


def test_an_auth_failure_on_the_first_key_never_moves_to_another(
    two_keys: Path, tmp_path: Path
) -> None:
    providers = _Providers(openrouter="unauthorized")
    result = _dispatch(tmp_path, providers)

    assert result["status"] == "failed", result
    assert GEMINI_URL not in providers.calls
