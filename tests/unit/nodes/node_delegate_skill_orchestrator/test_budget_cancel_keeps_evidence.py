# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation cancelled at the handler budget keeps the evidence it gathered.

C29 run 37977092319 (2026-10-09) was cancelled at ``stage=inference`` and its
receipt named no backend, no endpoint, no model, no key source and no attempt,
although ``stage=inference`` is reached only after routing chose a backend and
the provider call started. The cancel path built its terminal from nothing.
These tests drive the customer route as production wires it: the handler, the
local dispatch port and the effect in its own child process, with only the
provider faked. The first model answers 429, the call is re-aimed and the next
one never answers, and the budget cancels the run.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    current_dispatch_progress,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import byok_model_discovery as discovery
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    FakeModels,
    _isolated_effects,
    _row,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _install_ladder,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
_SHARED_AUTOUSE_FIXTURE = _isolated_effects

KEY = "synthetic-cancel-evidence-key-asserted-absent-from-outputs"
GEMMA = "google/gemma-4-31b-it:free"
NEMOTRON = "nvidia/nemotron-3-super-120b-a12b:free"
THROTTLE = f"{GEMMA} is temporarily rate-limited upstream"
BUDGET_SECONDS = 12


@pytest.fixture(autouse=True)
def short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise a fresh interpreter even on Linux. Child doubles must survive
    # the same spawn boundary production uses on macOS, without inheriting
    # parent monkeypatches. Leave time for both retry children to import.
    monkeypatch.setattr(
        port_mod,
        "_resolve_effect_process_context",
        lambda: multiprocessing.get_context("spawn"),
    )
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=BUDGET_SECONDS,
            terminal_delivery_margin_seconds=1,
        ),
    )


def _openrouter_url() -> str:
    return str(_row("openrouter").endpoint_url)


def _customer_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, Path]:
    """Register the customer's key and route the run onto their OpenRouter backend."""
    db_path = tmp_path / "delegation.sqlite"
    customer_ref = register_local_byok_credential(
        "openrouter", KEY, model=GEMMA, db_path=db_path
    )
    _install_ladder(monkeypatch, max_escalations=0)
    backend = ModelResolvedDelegationBackend(
        backend_id="byok-openrouter",
        model_id=GEMMA,
        endpoint_ref=_openrouter_url(),
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=60000,
        secret_ref=customer_ref,
    )
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_args, **_kwargs: backend
    )
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_args, **_kwargs: (SecretStr(KEY), EnumSecretSource.LOCAL_STORE),
    )
    return customer_ref, db_path


def _throttled(endpoint_url: str) -> transport.ModelTransportResponse:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", endpoint_url),
        json={"error": {"message": THROTTLE}},
    )
    response.raise_for_status()
    raise AssertionError("a 429 must raise")


def _throttle_then_hang_post(
    *, endpoint_url: str, payload: dict[str, Any], **_: Any
) -> transport.ModelTransportResponse:
    if payload["model"] == GEMMA:
        return _throttled(endpoint_url)
    time.sleep(60)
    raise AssertionError("the budget must end this call")


@dataclass
class _ReaimThenHangEffect:
    """Install the provider double inside the real effect's spawned child."""

    def __call__(self, request: Any) -> ModelLlmDelegationCallResult:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
            patch.setattr(effect, "_get_served_model_ids", lambda _: None)
            patch.setattr(
                effect,
                "resolve_api_key_with_source_loop_safe",
                lambda *_args, **_kwargs: (
                    SecretStr(KEY),
                    EnumSecretSource.LOCAL_STORE,
                ),
            )
            patch.setattr(
                discovery,
                "get_models_json",
                FakeModels({"data": [{"id": GEMMA}, {"id": NEMOTRON}]}),
            )
            patch.setattr(transport, "post_chat_completion", _throttle_then_hang_post)
            return effect.HandlerLlmDelegationCall()(request)


@dataclass
class _RetryThenHangEffect:
    first_call_marker: Path

    def __call__(self, request: Any) -> ModelLlmDelegationCallResult:
        if not self.first_call_marker.exists():
            self.first_call_marker.touch()
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class="rate_limited",
                error_message=THROTTLE,
                http_status=429,
                endpoint_healthy=True,
                secret_source=EnumSecretSource.LOCAL_STORE,
                secret_ref=request.secret_ref,
            )
        time.sleep(60)
        raise AssertionError("the budget must end this call")


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="explain what a calendar app needs",
        task_type="research",
        source="claude-code",
        correlation_id=uuid4(),
    )


async def _cancelled_terminal(port: port_mod.LocalDelegationDispatchPort) -> Any:
    request = _request()
    terminal = await asyncio.wait_for(
        HandlerDelegateSkill(dispatch_port=port).handle(request), timeout=30
    )
    assert terminal.correlation_id == request.correlation_id
    assert current_dispatch_progress.get() is None
    return terminal


def _assert_cancelled_with_routing_evidence(terminal: Any, customer_ref: str) -> None:
    assert terminal.status == "timeout"
    assert terminal.terminal_failure_cause == "timeout"
    assert "stage=inference" in terminal.error_message
    assert f"budget of {BUDGET_SECONDS}s" in terminal.error_message
    # The routing decision and the key that answered it.
    assert terminal.provider == _openrouter_url()
    assert terminal.secret_source is EnumSecretSource.LOCAL_STORE
    assert terminal.secret_ref == customer_ref
    for attempt in terminal.attempts:
        assert attempt.backend_id == "byok-openrouter"
        assert attempt.host == "openrouter.ai"
        assert attempt.tier == "cheap_cloud"
    assert terminal.attempts_count == len(terminal.attempts)
    assert KEY not in terminal.model_dump_json()


async def test_a_call_re_aimed_inside_the_effect_keeps_its_429_and_the_call_in_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 429 and the re-aimed model live in the effect's child process."""
    customer_ref, db_path = _customer_route(monkeypatch, tmp_path)
    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=_ReaimThenHangEffect(),
        evidence_db_path=db_path,
        effect_process_boundary=True,
    )

    terminal = await _cancelled_terminal(port)

    _assert_cancelled_with_routing_evidence(terminal, customer_ref)
    assert terminal.model_name == NEMOTRON
    assert [
        (a.model_id, a.failure_class, a.http_status) for a in terminal.attempts
    ] == [(GEMMA, "rate_limited", 429), (NEMOTRON, "timeout", None)]
    throttled, in_flight = terminal.attempts
    assert THROTTLE in throttled.error_message
    assert throttled.acceptance_decision == "climb"
    assert "stage=inference" in in_flight.error_message
    assert in_flight.acceptance_decision is None


async def test_a_retry_on_the_customer_route_keeps_the_earlier_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 429 came back to the port, which re-issued the same route."""
    customer_ref, db_path = _customer_route(monkeypatch, tmp_path)
    # Each call runs in a fresh child process, so the first call is told apart
    # by a file, not by state in this one.
    first_call_marker = tmp_path / "first-call-made"

    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=_RetryThenHangEffect(first_call_marker),
        evidence_db_path=db_path,
        effect_process_boundary=True,
    )

    terminal = await _cancelled_terminal(port)

    _assert_cancelled_with_routing_evidence(terminal, customer_ref)
    assert terminal.model_name == GEMMA
    assert [
        (a.model_id, a.failure_class, a.http_status) for a in terminal.attempts
    ] == [(GEMMA, "rate_limited", 429), (GEMMA, "timeout", None)]
