# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18079: the receipt names the model that ANSWERED, never the requested one.

Three cases on the local dispatch path (effect handler -> receipt) and the same
three on the inference-intent handler: the provider serves a different model
than requested, the provider reports nothing about the model, and a throttled
model is retried on another one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnimarket.handlers.routing import byok_model_discovery as discovery
from omnimarket.handlers.routing.byok_provider_backends import (
    resolve_byok_provider_backend,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.inference import secret_store_resolver
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as dispatch,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from tests.chains.local.harness import store_model_credential
from tests.test_omn19205_c29_byok_throttle_re_resolve import (
    GEMMA,
    NEMOTRON,
    OK_BODY,
    _bind,
    _request,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    _isolated_effects,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("catalogue_picks_models")]

_REQUESTED = "gemini-3.5-flash-lite"
_OTHER = "gemini-3.5-flash-other"
_REF = f"cred_acme_gemini_{'b' * 32}"
_KEY = "synthetic-omn18079-customer-key"
REF = f"cred_acme_openrouter_{'a' * 32}"
_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile."
)


def _ok(model: str | None) -> transport.ModelTransportResponse:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": _ANSWER}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
    }
    if model is not None:
        body["model"] = model
    return transport.ModelTransportResponse(
        status_code=200, json_body=body, latency_ms=1
    )


async def _receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    replies: list[transport.ModelTransportResponse],
    served_ids: frozenset[str] | None,
    listed: tuple[str, ...] = (_REQUESTED,),
) -> dict[str, object]:
    row = resolve_byok_provider_backend("gemini")
    assert row is not None
    backend = ModelResolvedDelegationBackend(
        backend_id=row.backend_id,
        model_id=_REQUESTED,
        endpoint_ref=row.endpoint_url,
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=5000,
        secret_ref=_REF,
    )
    monkeypatch.setattr(secret_store_resolver, "_configured_secret_store", lambda: None)
    monkeypatch.setattr(
        credentials, "default_evidence_db_path", lambda: tmp_path / "unused.sqlite"
    )
    monkeypatch.setattr(
        credentials.LocalByokCredentialStore,
        "get_secret",
        AsyncMock(return_value=_KEY),
    )
    monkeypatch.setattr(effect, "record_local_byok_model", Mock())
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: served_ids)
    monkeypatch.setattr(
        discovery,
        "get_models_json",
        lambda **_: {"models": [{"name": f"models/{m}"} for m in listed]},
    )
    queue = list(replies)
    monkeypatch.setattr(transport, "post_chat_completion", lambda **_: queue.pop(0))
    monkeypatch.setattr(
        dispatch, "resolve_delegation_backend", lambda *_a, **_k: backend
    )
    monkeypatch.setattr(dispatch, "RunStepRecorder", Mock())
    port = dispatch.LocalDelegationDispatchPort(
        effect_handler=effect.HandlerLlmDelegationCall(),
        evidence_db=Mock(),
        effect_process_boundary=False,
    )
    monkeypatch.setattr(port, "_project_evidence", Mock())
    monkeypatch.setattr(port, "_quota_snapshot", Mock(return_value=None))
    monkeypatch.setattr(port, "_deliver_quota_observation", Mock())
    return await port.dispatch(
        prompt="Explain the tradeoff and cite the evidence.",
        task_type="research",
        correlation_id=uuid4(),
        max_tokens=256,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=30,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="acme",
        backend_id="byok-gemini",
    )


async def test_different_served_model_is_named_not_the_requested_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = await _receipt(
        monkeypatch, tmp_path, replies=[_ok(_OTHER)], served_ids=None
    )
    assert receipt["status"] == "completed", receipt
    assert receipt["model_name"] == _OTHER


async def test_missing_serving_metadata_is_unknown_not_the_requested_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = await _receipt(
        monkeypatch, tmp_path, replies=[_ok(None)], served_ids=None
    )
    assert receipt["status"] == "completed", receipt
    assert receipt["model_name"] != _REQUESTED
    assert receipt["model_name"] == "unknown"


@pytest.fixture(autouse=True)
def _ref_is_catalogue_picked(tmp_path: Path) -> None:
    store_model_credential(
        tmp_path / "delegation.sqlite", REF, model=GEMMA, model_chosen_by="catalogue"
    )


_SHARED_AUTOUSE_FIXTURE = _isolated_effects


@pytest.mark.parametrize(
    ("second_body", "expected_served"),
    [
        (OK_BODY, None),
        ({**OK_BODY, "model": "vendor/third-model:free"}, "vendor/third-model:free"),
    ],
    ids=["no-metadata-is-unknown", "provider-names-a-third-model"],
)
def test_retry_landing_on_another_model_names_only_what_the_provider_reported(
    monkeypatch: pytest.MonkeyPatch,
    second_body: dict[str, Any],
    expected_served: str | None,
) -> None:
    provider, _ = _bind(monkeypatch, [429, second_body], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert result.success, result.error_message
    # The retry was REQUESTED on the second model ...
    assert [model for _url, model in provider.calls] == [GEMMA, NEMOTRON]
    # ... but the requested retry model is not serving evidence.
    assert result.served_model_id == expected_served
    assert result.served_model_id != NEMOTRON
    assert [a.model_id for a in result.earlier_model_attempts] == [GEMMA]


def test_completed_event_never_restates_the_requested_model_as_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, [200], [GEMMA])
    request = _request()
    result = effect.HandlerLlmDelegationCall().__call__(request)
    assert result.success, result.error_message
    event = effect.HandlerLlmDelegationCall()._build_completed_event(request, result)
    assert event.requested_model_id == GEMMA
    assert event.model_id == "unknown"
    assert event.selected_model == "unknown"


def test_inference_intent_names_reported_model_and_never_the_requested_one() -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
        _answering_model,
    )

    # different served model
    assert _answering_model({"model": _OTHER}, _REQUESTED, None) == _OTHER
    # missing metadata, no /v1/models evidence
    assert _answering_model({}, _REQUESTED, None) == ""
    assert _answering_model({"model": "  "}, _REQUESTED, None) == ""
    # /v1/models confirmed the requested id: that is serving evidence
    assert _answering_model({}, _REQUESTED, frozenset({_REQUESTED})) == _REQUESTED
    # a retry landed on another model and the provider says so
    assert (
        _answering_model({"model": _OTHER}, _REQUESTED, frozenset({_REQUESTED}))
        == _OTHER
    )


def test_receipt_records_the_provider_generation_id_when_it_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {**OK_BODY, "id": "gen-1764-abcdef", "model": "vendor/served:free"}
    _bind(monkeypatch, [body], [GEMMA])
    request = _request()
    result = effect.HandlerLlmDelegationCall().__call__(request)
    assert result.success, result.error_message
    assert result.provider_generation_id == "gen-1764-abcdef"
    event = effect.HandlerLlmDelegationCall()._build_completed_event(request, result)
    assert event.provider_generation_id == "gen-1764-abcdef"
    assert event.model_id == "vendor/served:free"


def test_missing_generation_id_reads_unknown_never_the_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {k: v for k, v in OK_BODY.items() if k not in ("id", "model")}
    _bind(monkeypatch, [body], [GEMMA])
    request = _request()
    result = effect.HandlerLlmDelegationCall().__call__(request)
    assert result.success, result.error_message
    assert result.provider_generation_id is None
    event = effect.HandlerLlmDelegationCall()._build_completed_event(request, result)
    assert event.provider_generation_id is None
    assert event.model_id == "unknown"
    assert event.model_id != GEMMA
