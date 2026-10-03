# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19205: provider health must not erase observed credential provenance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_api_key_with_source_loop_safe,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from tests.chains.local.harness import use_local_store
from tests.test_omn19205_c29_byok_throttle_re_resolve import (
    GEMMA,
    KEY,
    NEMOTRON,
    REF,
    FakeOpenRouter,
    _bind,
    _request,
    effect,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    _isolated_effects,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _dispatch,
    _install_ladder,
)

pytestmark = pytest.mark.unit
_SHARED_AUTOUSE_FIXTURE = _isolated_effects


@pytest.fixture
def recorded_429(monkeypatch: pytest.MonkeyPatch) -> FakeOpenRouter:
    recorded = json.loads(
        (
            Path(__file__).parent
            / "fixtures/omn19205/openrouter_free_tier_2026-10-02.json"
        ).read_text()
    )["responses"][0]
    assert recorded["model"] == GEMMA
    assert recorded["http_status"] == 429
    provider, _ = _bind(monkeypatch, [], [GEMMA, NEMOTRON])

    def post(
        *, endpoint_url: str, payload: dict[str, Any], **_: Any
    ) -> transport.ModelTransportResponse:
        provider.calls.append((endpoint_url, payload["model"]))
        response = httpx.Response(
            recorded["http_status"],
            request=httpx.Request("POST", endpoint_url),
            json=recorded["body"],
        )
        response.raise_for_status()
        raise AssertionError("the recorded response must fail")

    monkeypatch.setattr(transport, "post_chat_completion", post)
    return provider


def test_failed_customer_call_records_the_resolved_source(
    recorded_429: FakeOpenRouter,
) -> None:
    result = effect.HandlerLlmDelegationCall()(_request())

    assert result.success is False
    assert result.failure_class is Failure.RATE_LIMITED
    assert [model for _, model in recorded_429.calls] == [GEMMA, NEMOTRON]
    assert result.secret_source is EnumSecretSource.LOCAL_STORE
    assert result.secret_ref == REF
    assert KEY not in str(result.model_dump())


def test_failure_before_resolution_never_claims_a_source(
    monkeypatch: pytest.MonkeyPatch, recorded_429: FakeOpenRouter
) -> None:
    def absent(*_args: Any, **_kwargs: Any) -> Any:
        raise SecretResolutionError("the declared credential is absent")

    monkeypatch.setattr(effect, "resolve_api_key_with_source_loop_safe", absent)
    result = effect.HandlerLlmDelegationCall()(_request())

    assert result.success is False
    assert result.failure_class is Failure.PROVIDER_CREDENTIAL_MISSING
    assert recorded_429.calls == []
    assert result.secret_source is None
    assert result.secret_ref is None
    assert KEY not in str(result.model_dump())


def test_failed_terminal_records_the_registered_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorded_429: FakeOpenRouter
) -> None:
    db_path = use_local_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        resolve_api_key_with_source_loop_safe,
    )
    customer_ref = register_local_byok_credential(
        "openrouter", KEY, model=GEMMA, db_path=db_path
    )
    # The chain harness's final successor is keyless, so its absent source is
    # honest. Bind the port's final attempt to the registered customer route.
    _install_ladder(monkeypatch, max_escalations=0)
    request = _request(ref=customer_ref)
    backend = ModelResolvedDelegationBackend(
        backend_id="local-coder",
        model_id=GEMMA,
        endpoint_ref=request.endpoint_ref,
        tier="local",
        max_tokens=256,
        timeout_ms=5000,
        secret_ref=customer_ref,
    )
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_args, **_kwargs: backend
    )
    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=effect.HandlerLlmDelegationCall(),
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    terminal = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert terminal["status"] == "failed"
    assert [model for _, model in recorded_429.calls] == [GEMMA, NEMOTRON]
    assert terminal.get("secret_source") == EnumSecretSource.LOCAL_STORE.value
    assert terminal.get("secret_ref") == customer_ref
    assert KEY not in str(terminal)
