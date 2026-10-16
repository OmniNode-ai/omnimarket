# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157 AC5: a store-backed Gemini BYOK receipt names the served model.

The real lab delegation is evidenced elsewhere. These hosted tests exercise
the call effect and receipt assembly with synthetic credentials and HTTP stubs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4

import httpx
import pytest

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.inference import secret_store_resolver
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as dispatch,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import byok_model_discovery as discovery
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    resolve_byok_provider_backend,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = pytest.mark.unit

_MODEL = "gemini-3.5-flash-lite"
_REF = f"cred_acme_gemini_{'a' * 32}"
_KEY = "synthetic-omn20157-customer-key"
_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile."
)


async def _receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, initial_model: str
) -> dict[str, object]:
    row = resolve_byok_provider_backend("gemini")
    assert row is not None
    assert row.backend_id == "byok-gemini"
    backend = ModelResolvedDelegationBackend(
        backend_id=row.backend_id,
        model_id=initial_model,
        endpoint_ref=row.endpoint_url,
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=5000,
        secret_ref=_REF,
    )

    # Keep the real resolver's source selection and tenant-ref handling. Only
    # the store read is faked: no SQLite, operator configuration or real keys.
    monkeypatch.setattr(secret_store_resolver, "_configured_secret_store", lambda: None)
    monkeypatch.setattr(
        credentials, "default_evidence_db_path", lambda: tmp_path / "unused.sqlite"
    )
    store_read = AsyncMock(return_value=_KEY)
    monkeypatch.setattr(credentials.LocalByokCredentialStore, "get_secret", store_read)
    monkeypatch.setattr(effect, "record_local_byok_model", Mock())
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: frozenset({_MODEL}))

    posts: list[str] = []
    model_lists: list[str] = []

    def get_models(
        *, url: str, extra_headers: dict[str, str], **_: Any
    ) -> dict[str, object]:
        assert url == row.models_url
        assert extra_headers == {"Authorization": f"Bearer {_KEY}"}
        model_lists.append(url)
        return {"models": [{"name": f"models/{_MODEL}"}]}

    def post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        **_: Any,
    ) -> transport.ModelTransportResponse:
        assert endpoint_url == row.endpoint_url
        assert payload["model"] == _MODEL
        assert extra_headers["Authorization"] == f"Bearer {_KEY}"
        posts.append(str(payload["model"]))
        response = httpx.Response(
            200,
            request=httpx.Request("POST", endpoint_url),
            json={
                "model": _MODEL,
                "choices": [{"message": {"content": _ANSWER}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            },
        )
        return transport.ModelTransportResponse(
            status_code=response.status_code,
            json_body=response.json(),
            latency_ms=1,
        )

    monkeypatch.setattr(discovery, "get_models_json", get_models)
    monkeypatch.setattr(transport, "post_chat_completion", post)
    monkeypatch.setattr(
        dispatch, "resolve_delegation_backend", lambda *_args, **_kwargs: backend
    )
    # Persistence, run-trace emission and quota projections are outside this
    # receipt test. Keep the real dispatch/effect/gate/receipt handoff in-process.
    monkeypatch.setattr(dispatch, "RunStepRecorder", Mock())
    port = dispatch.LocalDelegationDispatchPort(
        effect_handler=effect.HandlerLlmDelegationCall(),
        evidence_db=Mock(),
        effect_process_boundary=False,
    )
    monkeypatch.setattr(port, "_project_evidence", Mock())
    monkeypatch.setattr(port, "_quota_snapshot", Mock(return_value=None))
    monkeypatch.setattr(port, "_deliver_quota_observation", Mock())
    receipt = await port.dispatch(
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
    assert posts == [_MODEL]
    expected_reads = 2 if initial_model == BYOK_MODEL_UNRESOLVED else 1
    assert store_read.await_args_list == [call(_REF)] * expected_reads
    assert model_lists == (
        [row.models_url] if initial_model == BYOK_MODEL_UNRESOLVED else []
    )
    assert _KEY not in str(receipt)
    return receipt


async def test_configured_gemini_model_is_named_on_store_backed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = await _receipt(monkeypatch, tmp_path, initial_model=_MODEL)

    assert receipt["status"] == "completed", receipt
    assert receipt["model_name"] == _MODEL
    assert receipt["secret_source"] == EnumSecretSource.LOCAL_STORE.value == "store"
    assert receipt["secret_ref"] == _REF
    attempts = receipt["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    assert attempts[0]["backend_id"] == "byok-gemini"
    assert attempts[0]["model_id"] == _MODEL
    assert attempts[0]["http_status"] == 200


async def test_discovered_gemini_model_replaces_unresolved_name_on_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = await _receipt(monkeypatch, tmp_path, initial_model=BYOK_MODEL_UNRESOLVED)

    assert receipt["status"] == "completed", receipt
    assert receipt["model_name"] == _MODEL
    assert receipt["secret_source"] == EnumSecretSource.LOCAL_STORE.value == "store"
    assert receipt["secret_ref"] == _REF
    attempts = receipt["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    assert attempts[0]["backend_id"] == "byok-gemini"
    assert attempts[0]["model_id"] == _MODEL
    assert attempts[0]["http_status"] == 200
