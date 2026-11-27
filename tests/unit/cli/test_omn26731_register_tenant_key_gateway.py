# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A signed-in customer registers a provider key through the gateway (OMN-26731).

One test body, the transport injected (ruling 2026-11-27T03:29:55Z): the fixture
hands every test an ``httpx.Client`` bound to a gateway. The unit run drives the
real gateway app with a fake identity and fake stores through the client's own
ASGI transport; the integration run sets ``ONEX_DEV_GATEWAY_URL`` plus the two
sign-in keys (``ONEX_DEV_GATEWAY_KEY_A``, ``ONEX_DEV_GATEWAY_KEY_B``) and drives
the dev gateway with the same bodies.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import SecretStr

from omnimarket.cli import cli_secret
from omnimarket.handlers.routing.byok_model_discovery import ModelByokModelDiscovery
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_tenant_key_register import (
    HandlerTenantKeyRegister,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import build_gateway

pytestmark = pytest.mark.unit

_VALUE = "sk-or-v1-test-customer-key-never-printed-or-stored-in-the-clear"
_TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_KEY_A = "onxk_test_a"
_KEY_B = "onxk_test_b"
_DEV_URL_ENV = "ONEX_DEV_GATEWAY_URL"


class _FakeBus:
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


class _FakeSecretStore:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


class _FakeIdentity:
    """Resolves the two sign-in keys to their tenants, never by slug."""

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        presented = headers.get("x-api-key", "")
        tenant = {_KEY_A: _TENANT_A, _KEY_B: _TENANT_B}.get(presented)
        if tenant is None:
            return None
        return ModelHttpTenantIdentity(
            tenant_id=tenant, tenant_slug=f"slug-{tenant.hex[:4]}", principal_id="u-1"
        )


async def _listed(*_a: Any, **_k: Any) -> ModelByokModelDiscovery:
    return ModelByokModelDiscovery(
        provider="openrouter", plan="default", outcome="resolved", model="m/chosen"
    )


@dataclass
class Gateway:
    """The injected transport plus what the fake gateway wrote, when it is fake."""

    client: httpx.Client
    base_url: str
    key_a: str
    key_b: str
    tenant_a: str | None
    tenant_b: str | None
    store: _FakeSecretStore | None
    bus: _FakeBus | None


def _fake_gateway() -> Iterator[Gateway]:
    store, bus = _FakeSecretStore(), _FakeBus()
    handler = HandlerTenantKeyRegister(
        secret_store=store, event_bus=bus, model_discoverer=_listed
    )
    deps = replace(
        build_gateway().deps,
        identity=_FakeIdentity(),
        api_key_header="x-api-key",
        tenant_keys=handler,
    )
    with TestClient(create_app(dependencies=deps), base_url="http://gateway") as client:
        yield Gateway(
            client,
            "http://gateway",
            _KEY_A,
            _KEY_B,
            str(_TENANT_A),
            str(_TENANT_B),
            store,
            bus,
        )


def _dev_gateway() -> Iterator[Gateway]:
    url = os.environ[_DEV_URL_ENV]
    with httpx.Client(base_url=url, timeout=60.0) as client:
        yield Gateway(
            client,
            url,
            os.environ["ONEX_DEV_GATEWAY_KEY_A"],
            os.environ["ONEX_DEV_GATEWAY_KEY_B"],
            None,
            None,
            None,
            None,
        )


_PARAMS = ["fake", *(["dev"] if os.environ.get(_DEV_URL_ENV) else [])]


@pytest.fixture(params=_PARAMS)
def gateway(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Gateway]:
    source = _fake_gateway() if request.param == "fake" else _dev_gateway()
    gw = next(source)
    # A clean customer machine: no broker settings, an empty home.
    for name in ("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BROKER", "ONEX_LAB_RUN_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        cli_secret,
        "_signed_in_gateway",
        lambda: (gw.base_url, SecretStr(gw.key_a), gw.client),
    )
    monkeypatch.setattr(
        cli_secret,
        "_tenant_key_event_bus",
        lambda: pytest.fail("the bus must not be touched when signed in"),
    )
    yield gw
    next(source, None)


def _register(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        ["register-tenant-key", "openrouter", "--model", "m/chosen", *extra],
        input=f"{_VALUE}\n",
    )


def test_signed_in_register_needs_no_broker_and_stores_under_the_tenant_uuid(
    gateway: Gateway,
) -> None:
    result = _register()
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["provider"] == "openrouter"
    assert shown["models"] == ["m/chosen"]
    assert _VALUE not in result.output
    if gateway.store is not None and gateway.bus is not None:
        ref = shown["api_key_ref"]
        assert ref.startswith(f"cred_{gateway.tenant_a}_openrouter_")
        assert gateway.store.secrets == {ref: _VALUE}
        assert len(gateway.bus.events) == 1
        assert _VALUE not in gateway.bus.events[0]


def test_a_request_for_another_tenant_is_refused_and_stores_nothing(
    gateway: Gateway,
) -> None:
    other = gateway.tenant_b or "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    result = _register("--tenant", other)
    assert result.exit_code != 0
    assert _VALUE not in result.output
    assert "another tenant" in result.output
    if gateway.store is not None and gateway.bus is not None:
        assert gateway.store.secrets == {}
        assert gateway.bus.events == []


def test_the_gateway_refuses_a_body_naming_another_tenant_directly(
    gateway: Gateway,
) -> None:
    body = {
        "tenant": gateway.tenant_b or "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "provider": "openrouter",
        "name": "openrouter",
        "key_value": _VALUE,
        "models": ["m/chosen"],
    }
    response = gateway.client.post(
        "/v1/workflows/tenant-keys", json=body, headers={"x-api-key": gateway.key_a}
    )
    assert response.status_code == 403
    assert _VALUE not in response.text
    if gateway.store is not None:
        assert gateway.store.secrets == {}


def test_an_unknown_sign_in_is_refused_before_the_body_is_read(
    gateway: Gateway,
) -> None:
    response = gateway.client.post(
        "/v1/workflows/tenant-keys",
        json={"provider": "openrouter", "key_value": _VALUE},
        headers={"x-api-key": "onxk_not_a_key"},
    )
    assert response.status_code == 401
    if gateway.store is not None:
        assert gateway.store.secrets == {}
