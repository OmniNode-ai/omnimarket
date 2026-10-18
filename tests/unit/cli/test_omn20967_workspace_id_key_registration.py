# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Claude API key carries its workspace id from registration to every request (OMN-20967).

Each company Claude API key belongs to one Anthropic workspace and is answered
only when the request names it in the ``anthropic-workspace-id`` header.
``register-tenant-key --workspace-id`` records the id with the key (the request,
the credential-registered event, the catalog row), and the review gateway puts
it on the delegation call it makes with that key. The id is not a secret, but it
is injected into a header, so its charset is closed.

Fakes only: the provider's model list is a stub and no network is used.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr, ValidationError

from omnimarket.cli import cli_secret
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    select_host_keys,
    translate_chat_request,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as call_handler,
)
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelInferenceCredentialCreateRequest,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_model_discovery import (
    discover_byok_model_sync,
    models_request_headers,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

pytestmark = pytest.mark.unit

_KEY = "sk-ant-synthetic-omn20967-key-never-printed"
_WORKSPACE = "wrkspc_01SyntheticWorkspaceId"
#: The header Anthropic documents for a workspace's key; the catalogue row must
#: declare exactly this, and every request below must carry it.
ANTHROPIC_WORKSPACE_HEADER = "anthropic-workspace-id"
_MODEL = "claude-haiku-4-5"
_TENANT = "omninode"
_HOST = "h201"


class _Models:
    """The provider's list-models endpoint, as a stub that records each read."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []

    def __call__(self, *, url: str, **kwargs: Any) -> Any:
        self.headers.append(dict(kwargs.get("extra_headers") or {}))
        return {"data": [{"id": _MODEL}]}


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        self.events.append(envelope.model_dump_json())


@pytest.fixture
def intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalByokCredentialStore, _Bus, _Models]:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    bus = _Bus()
    models = _Models()
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)
    return store, bus, models


def _register(provider: str, *extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            provider,
            "--tenant",
            _TENANT,
            "--expires",
            "2099-01-01",
            *extra,
        ],
        input=f"{_KEY}\n",
    )


# --- the request model ---------------------------------------------------------


def test_the_request_accepts_a_workspace_id_for_an_anthropic_key() -> None:
    request = ModelInferenceCredentialCreateRequest(
        name="k",
        provider="anthropic",
        key_value=SecretStr(_KEY),
        workspace_id=_WORKSPACE,
    )
    assert request.workspace_id == _WORKSPACE


def test_the_request_refuses_a_workspace_id_for_another_provider() -> None:
    with pytest.raises(ValidationError) as refused:
        ModelInferenceCredentialCreateRequest(
            name="k",
            provider="openrouter",
            key_value=SecretStr(_KEY),
            workspace_id=_WORKSPACE,
        )
    assert "workspace" in str(refused.value).lower()


@pytest.mark.parametrize(
    "bad", ["", " ", "wrk spc", "wrk\r\nx-evil: 1", "wrk;spc", "w" * 129, "-leading"]
)
def test_the_request_refuses_a_workspace_id_that_cannot_ride_in_a_header(
    bad: str,
) -> None:
    with pytest.raises(ValidationError):
        ModelInferenceCredentialCreateRequest(
            name="k", provider="anthropic", key_value=SecretStr(_KEY), workspace_id=bad
        )


def test_the_registered_event_carries_the_workspace_id() -> None:
    event = ModelCredentialRegisteredEvent(
        tenant_id=_TENANT,
        provider="anthropic",
        name="k",
        api_key_ref="cred_omninode_anthropic_x",
        workspace_id=_WORKSPACE,
    )
    assert event.workspace_id == _WORKSPACE
    assert ModelCredentialRegisteredEvent.model_fields["workspace_id"].default is None


# --- the provider's own reads --------------------------------------------------


def test_the_catalogue_declares_the_anthropic_workspace_header_and_no_other_does() -> (
    None
):
    anthropic = resolve_byok_provider_backend("anthropic")
    assert anthropic is not None
    assert anthropic.workspace_header == ANTHROPIC_WORKSPACE_HEADER
    openrouter = resolve_byok_provider_backend("openrouter")
    assert openrouter is not None
    assert openrouter.workspace_header is None


def test_the_models_read_names_the_workspace_when_the_key_has_one() -> None:
    backend = resolve_byok_provider_backend("anthropic")
    assert backend is not None
    headers = models_request_headers(backend, _KEY, workspace_id=_WORKSPACE)
    assert headers[ANTHROPIC_WORKSPACE_HEADER] == _WORKSPACE
    assert headers["x-api-key"] == _KEY


def test_the_models_read_names_no_workspace_when_the_key_has_none() -> None:
    backend = resolve_byok_provider_backend("anthropic")
    assert backend is not None
    assert ANTHROPIC_WORKSPACE_HEADER not in models_request_headers(backend, _KEY)


def test_discovery_forwards_the_workspace_to_the_models_read() -> None:
    backend = resolve_byok_provider_backend("anthropic")
    assert backend is not None
    models = _Models()
    found = discover_byok_model_sync(backend, _KEY, workspace_id=_WORKSPACE, get=models)
    assert found.outcome == "resolved"
    assert models.headers[0][ANTHROPIC_WORKSPACE_HEADER] == _WORKSPACE


# --- register-tenant-key --workspace-id ---------------------------------------


def test_register_tenant_key_records_the_workspace_id_everywhere(
    intake: tuple[LocalByokCredentialStore, _Bus, _Models],
) -> None:
    store, bus, models = intake
    result = _register("anthropic", "--workspace-id", _WORKSPACE)
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["workspace_id"] == _WORKSPACE
    assert _KEY not in result.output
    assert len(bus.events) == 1
    assert json.loads(bus.events[0])["payload"]["workspace_id"] == _WORKSPACE
    # the registration-time model read already names the workspace
    assert models.headers
    assert all(h[ANTHROPIC_WORKSPACE_HEADER] == _WORKSPACE for h in models.headers)
    # this machine's catalog row, which the host's review gateway reads
    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    rows = SqliteDatabaseAdapter(store.db_path).query(
        "tenant_inference_credentials", {"api_key_ref": shown["api_key_ref"]}
    )
    assert [row["workspace_id"] for row in rows] == [_WORKSPACE]
    assert asyncio.run(store.get_secret(shown["api_key_ref"])) == _KEY


def test_register_tenant_key_without_a_workspace_id_is_unchanged(
    intake: tuple[LocalByokCredentialStore, _Bus, _Models],
) -> None:
    _store, bus, models = intake
    result = _register("anthropic")
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert "workspace_id" not in shown
    assert json.loads(bus.events[0])["payload"]["workspace_id"] is None
    assert all(ANTHROPIC_WORKSPACE_HEADER not in h for h in models.headers)


def test_register_tenant_key_refuses_a_workspace_id_for_another_provider(
    intake: tuple[LocalByokCredentialStore, _Bus, _Models],
) -> None:
    store, bus, _ = intake
    result = _register("glm", "--workspace-id", _WORKSPACE)
    assert result.exit_code != 0
    assert "workspace" in result.output.lower()
    assert _KEY not in result.output
    assert bus.events == []
    assert asyncio.run(store.list_keys()) == []


def test_register_tenant_key_refuses_a_workspace_id_with_a_header_break(
    intake: tuple[LocalByokCredentialStore, _Bus, _Models],
) -> None:
    store, bus, _ = intake
    result = _register("anthropic", "--workspace-id", "wrk\r\nx-evil: 1")
    assert result.exit_code != 0
    assert bus.events == []
    assert asyncio.run(store.list_keys()) == []


# --- the review gateway --------------------------------------------------------


def _row(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "api_key_ref": "cred_omninode_anthropic_a",
        "tenant_id": _TENANT,
        "provider": "anthropic",
        "name": "a",
        "bound_host": _HOST,
        "revoked_at": None,
        "expires_at": None,
    }
    row.update(extra)
    return row


def _now() -> datetime:
    return datetime(2026, 10, 12, tzinfo=UTC)


def test_the_gateway_key_carries_the_catalogued_workspace_id() -> None:
    [with_ws] = select_host_keys(
        [_row(workspace_id=_WORKSPACE)], tenant_id=_TENANT, host=_HOST, now=_now()
    )
    [without] = select_host_keys([_row()], tenant_id=_TENANT, host=_HOST, now=_now())
    assert with_ws.workspace_id == _WORKSPACE
    assert without.workspace_id is None


def _translate(workspace_id: str | None) -> ModelLlmDelegationCallRequest:
    return translate_chat_request(
        {"messages": [{"role": "user", "content": "review"}]},
        model="claude-sonnet-5-5",
        endpoint_url="https://provider.invalid/v1/chat/completions",
        secret_ref="cred_omninode_anthropic_a",
        timeout_seconds=30.0,
        workspace_id=workspace_id,
    )


def test_the_delegation_call_names_the_workspace_in_its_headers() -> None:
    request = _translate(_WORKSPACE)
    assert request.extra_headers == {ANTHROPIC_WORKSPACE_HEADER: _WORKSPACE}


def test_the_delegation_call_adds_no_header_for_a_key_with_no_workspace() -> None:
    assert _translate(None).extra_headers == {}


def test_a_model_list_read_during_a_call_keeps_the_calls_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 404 re-resolution reads the key's model list: same key, same workspace."""
    seen: dict[str, Any] = {}

    def fake_discover(backend: Any, api_key: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return byok_model_discovery.ModelByokModelDiscovery(
            provider="anthropic", plan="api", outcome="resolved", model=_MODEL
        )

    monkeypatch.setattr(call_handler, "discover_byok_model_sync", fake_discover)
    monkeypatch.setattr(
        call_handler,
        "resolve_api_key_with_source_loop_safe",
        lambda *_a, **_k: (SecretStr(_KEY), None),
    )
    backend = resolve_byok_provider_backend("anthropic")
    assert backend is not None
    resolved = call_handler.HandlerLlmDelegationCall()._resolve_byok_model(
        _translate(_WORKSPACE), backend, exclude=(), remember=False
    )
    assert isinstance(resolved, ModelLlmDelegationCallRequest)
    assert seen["workspace_id"] == _WORKSPACE
