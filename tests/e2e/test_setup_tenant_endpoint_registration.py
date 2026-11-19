# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical publisher and both projection writers preserve endpoint identity.

Controlled real HTTP model catalogs verify exact key-to-server binding. SQLite
is temporary; the deployed writer's SQL transport is injected, never Postgres.
Failures: placeholder catalog URL, wrong-server key, collapsed backend identity,
unsafe metadata, hosted-provider URL substitution and secrets on events/results.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    ProtocolCredentialEventBus,
    register_inference_credential,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.e2e


@contextmanager
def endpoint(expected: str, model: str):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            matched = self.headers.get("Authorization") == "Bearer " + expected
            seen.append({"path": self.path, "correct_key": matched})
            body = json.dumps(
                {"data": [{"id": model}]}
                if matched
                else {"error": {"message": "unauthorized"}}
            ).encode()
            self.send_response(200 if matched else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.asyncio
async def test_named_endpoint_registration_reaches_canonical_tenant_route(
    tmp_path: Path,
) -> None:
    store = AsyncMock()
    store.set_secret.return_value = True
    bus = AsyncMock(spec=ProtocolCredentialEventBus)
    db = SqliteDatabaseAdapter(tmp_path / "tenant.sqlite")
    outcomes = []
    with (
        endpoint("fixture-key-one", "model-one") as (url1, seen1),
        endpoint("fixture-key-two", "model-two") as (url2, seen2),
    ):
        for connection, url, key, model in [
            ("one", url1, "fixture-key-one", "model-one"),
            ("two", url2, "fixture-key-two", "model-two"),
        ]:
            request = ModelInferenceCredentialCreateRequest(
                name=connection,
                provider="openai_compatible",
                key_value=key,
                models=[model],
                base_url=url,
                connection_id=connection,
            )
            result = await register_inference_credential(
                request,
                tenant_id="tenant-" + connection,
                secret_store=store,
                event_bus=bus,
                allow_custom_endpoint=True,
            )
            event = bus.publish_envelope.await_args.args[0].payload.model_dump(
                mode="json"
            )
            assert event["metadata"]["base_url"] == url
            assert event["metadata"]["connection_id"] == connection
            assert key not in json.dumps(event)
            assert key not in result.model_dump_json()
            assert apply_credential_registered(event, db)
            rows = db.query(
                "delegation_routing_tenant_overlay",
                {"tenant_id": "tenant-" + connection},
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["endpoint_url"] == url + "/v1/chat/completions"
            assert row["model_name"] == model
            assert row["secret_ref"] == result.api_key_ref
            assert row["backend_id"].endswith("-" + connection)
            outcomes.append(
                {
                    "tenant": "tenant-" + connection,
                    "url": row["endpoint_url"],
                    "backend": row["backend_id"],
                    "model": model,
                }
            )
        assert seen1
        assert seen2
        assert all(x["correct_key"] for x in seen1 + seen2)
    assert (
        store.set_secret.await_args_list[0].args[0]
        != store.set_secret.await_args_list[1].args[0]
    )
    (tmp_path / "tenant-endpoint-proof.json").write_text(
        json.dumps(
            {
                "proof_kind": "real catalog HTTP; SQLite projection; no Docker or model inference",
                "routes": outcomes,
                "catalog_requests": seen1 + seen2,
            },
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@example.test",
        "https://example.test?token=secret",
        "file:///tmp/model",
        "https://example.test/#secret",
    ],
)
def test_unsafe_endpoint_registration_refused(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ModelInferenceCredentialCreateRequest(
            name="test",
            provider="openai_compatible",
            key_value="fixture",
            models=["model"],
            base_url=base_url,
            connection_id="test",
        )


def test_hosted_provider_cannot_be_redirected() -> None:
    with pytest.raises(ValidationError):
        ModelInferenceCredentialCreateRequest(
            name="test",
            provider="openai",
            key_value="fixture",
            models=["model"],
            base_url="http://127.0.0.1:1234",
            connection_id="test",
        )


def test_untrusted_event_url_is_rejected_before_catalog_write(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "tenant.sqlite")
    event = {
        "api_key_ref": "cred_tenant_one",
        "tenant_id": "tenant",
        "provider": "openai",
        "name": "redirect",
        "metadata": {"base_url": "http://127.0.0.1:1234", "connection_id": "redirect"},
        "models": ["model"],
    }
    with pytest.raises(ValueError, match="does not allow"):
        apply_credential_registered(event, db)
    assert db.query("tenant_inference_credentials", {"tenant_id": "tenant"}) == []


@pytest.mark.asyncio
async def test_deployed_projection_uses_registered_url_and_connection() -> None:
    from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
        HandlerTenantCredentialsProjectionRunner,
    )
    from omnimarket.projection.runner import MessageMeta

    runner = HandlerTenantCredentialsProjectionRunner()
    db = AsyncMock()
    db.execute.return_value = []
    runner._db = db
    event = {
        "api_key_ref": "cred_tenant_custom",
        "tenant_id": "tenant-custom",
        "provider": "openai_compatible",
        "name": "custom",
        "metadata": {
            "base_url": "https://model.example.test",
            "connection_id": "custom",
        },
        "models": ["new-model"],
    }
    await runner.project_event(
        "onex.evt.omnimarket.credential-registered.v1",
        event,
        MessageMeta(partition=0, offset=0, fallback_id="fixture"),
    )
    calls = [
        call
        for call in db.execute.await_args_list
        if "INSERT INTO delegation_routing_tenant_overlay" in call.args[0]
    ]
    assert len(calls) == 1
    assert "https://model.example.test/v1/chat/completions" in calls[0].args
    assert "byok-openai-compatible-custom" in calls[0].args
    assert calls[0].kwargs["tenant"] == "tenant-custom"


def test_custom_endpoint_never_pools_a_different_servers_key(tmp_path: Path) -> None:
    from omnimarket.routing.tenant_overlay_resolver import resolve_tenant_overlay

    db = SqliteDatabaseAdapter(tmp_path / "tenant.sqlite")
    for name in ("first", "second"):
        event = {
            "api_key_ref": "cred_tenant_" + name,
            "tenant_id": "tenant",
            "provider": "openai_compatible",
            "name": name,
            "metadata": {
                "base_url": "https://" + name + ".example.test",
                "connection_id": name,
            },
            "models": [name + "-model"],
        }
        apply_credential_registered(event, db)
    for index in range(32):
        selected = resolve_tenant_overlay(
            db, tenant_id="tenant", task_type="coding", selector_key=str(index)
        )
        assert selected is not None
        assert selected.secret_ref == "cred_tenant_second"
        assert (
            selected.endpoint_url == "https://second.example.test/v1/chat/completions"
        )
    apply_credential_registered(
        {**event, "expires_at": "2000-01-01T00:00:00+00:00"}, db
    )
    expired = resolve_tenant_overlay(
        db, tenant_id="tenant", task_type="coding", selector_key="expired"
    )
    assert expired is not None
    assert expired.secret_ref is None, (
        "expired custom key must not select another server key"
    )


def test_cli_custom_tenant_key_uses_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from omnimarket.cli import cli_secret
    from omnimarket.inference.local_byok_credential_adapter import (
        LocalByokCredentialStore,
    )

    store = LocalByokCredentialStore(db_path=tmp_path / "keys.sqlite")
    bus = AsyncMock(spec=ProtocolCredentialEventBus)
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli_secret, "_resolve_bound_host", lambda _host: None)
    monkeypatch.setattr(cli_secret, "_lab_host_bus_lane", lambda _host: None)
    with endpoint("fixture-cli-key", "server-model") as (url, seen):
        result = CliRunner().invoke(
            cli_secret.secret_group,
            [
                "register-tenant-key",
                "openai_compatible",
                "--tenant",
                "fixture-tenant",
                "--name",
                "chosen",
                "--base-url",
                url,
                "--connection-id",
                "chosen",
                "--model",
                "server-model",
            ],
            input="fixture-cli-key\n",
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["base_url"] == url
        assert body["connection_id"] == "chosen"
        rows = SqliteDatabaseAdapter(store.db_path).query(
            "delegation_routing_tenant_overlay", {"tenant_id": "fixture-tenant"}
        )
        assert rows[0]["endpoint_url"] == url + "/v1/chat/completions"
        assert seen
        assert all(x["correct_key"] for x in seen)
    assert "fixture-cli-key" not in result.output


@pytest.mark.asyncio
async def test_hosted_intake_does_not_gain_custom_url_network_access() -> None:
    from omnimarket.handlers.routing.byok_model_discovery import ModelByokModelDiscovery

    request = ModelInferenceCredentialCreateRequest(
        name="custom",
        provider="openai_compatible",
        key_value="fixture",
        models=["model"],
        base_url="http://127.0.0.1:2345",
        connection_id="custom",
    )
    discover = AsyncMock(
        return_value=ModelByokModelDiscovery(
            provider="openai_compatible",
            plan="by_url",
            outcome="resolved",
            model="model",
        )
    )
    store = AsyncMock()
    store.set_secret.return_value = True
    with pytest.raises(ValueError, match="local endpoint registration"):
        await register_inference_credential(
            request,
            tenant_id="tenant",
            secret_store=store,
            event_bus=AsyncMock(spec=ProtocolCredentialEventBus),
            model_discoverer=discover,
        )
    discover.assert_not_called()
    store.set_secret.assert_not_called()
