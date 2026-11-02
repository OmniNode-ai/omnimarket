# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-mounted webhook door and its existing delivery consumer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import http_app
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.handler_github_webhook_http import (
    HandlerGitHubWebhookHttp,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.http_app import (
    create_app,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.http_dependencies import (
    BusWebhookPublisher,
    EnvironmentWebhookConfiguration,
    load_http_config,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_webhook_http import (
    ModelWebhookTarget,
)

pytestmark = pytest.mark.unit
SIGNING_KEY = b"webhook-fixture-only"
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
BODY = b'{ "action": "opened", "number": 7 }'
TARGET = ModelWebhookTarget(
    tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
    tenant_slug="fixture-tenant",
    principal_id="fixture-principal",
)


class Configuration:
    def __init__(self, secret=SIGNING_KEY, target=TARGET):
        self.secret = secret
        self.target = target

    async def resolve(self):
        return self.secret, self.target


class Publisher:
    def __init__(self):
        self.records = []
        self.fail = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def publish(self, *, topic, key, value):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("fixture publish refusal")
        self.records.append((topic, key, json.loads(value)))


@pytest.fixture
def publisher():
    return Publisher()


@pytest.fixture
async def client(publisher):
    handler = HandlerGitHubWebhookHttp(
        configuration=Configuration(), publisher=publisher, config=load_http_config()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(handler=handler)),
        base_url="http://fixture.invalid",
    ) as client:
        yield client


def headers(body=BODY, event="pull_request"):
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": DELIVERY,
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest(),
    }


async def test_accepts_exact_bytes_and_publishes_existing_consumer_payload(
    client, publisher
):
    response = await client.post("/v1/github/webhook", content=BODY, headers=headers())
    assert response.status_code == 202
    assert response.json() == {"ok": True, "delivery_id": DELIVERY}
    ((topic, key, envelope),) = publisher.records
    assert topic == load_http_config().delivery_topic
    assert key == DELIVERY.encode()
    assert envelope["correlation_id"] == DELIVERY
    assert envelope["tenant_id"] == TARGET.tenant_slug
    assert envelope["payload"]["tenant_id"] == TARGET.tenant_slug
    assert base64.b64decode(envelope["payload"]["body_b64"]) == BODY
    assert envelope["metadata"]["tags"]["source_tenant_id"] == str(TARGET.tenant_id)


async def test_ping_verifies_signature_but_publishes_nothing(client, publisher):
    response = await client.post(
        "/v1/github/webhook", content=BODY, headers=headers(event="ping")
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "event": "ping"}
    assert publisher.records == []


@pytest.mark.parametrize(
    ("change", "status", "error"),
    [
        ({"X-GitHub-Event": ""}, 400, "missing GitHub delivery headers"),
        ({"X-Hub-Signature-256": ""}, 400, "missing GitHub delivery headers"),
        ({"X-GitHub-Delivery": "invalid"}, 400, "delivery id is not a GUID"),
        ({"X-Hub-Signature-256": "sha256=bad"}, 401, "signature does not verify"),
    ],
)
async def test_refusals_do_not_publish(client, publisher, change, status, error):
    response = await client.post(
        "/v1/github/webhook", content=BODY, headers=headers() | change
    )
    assert response.status_code == status
    assert response.json() == {"ok": False, "error": error}
    assert publisher.records == []


@pytest.mark.parametrize(("secret", "target"), [(b"", TARGET), (SIGNING_KEY, None)])
async def test_missing_configuration_precedes_header_validation(
    secret, target, publisher
):
    handler = HandlerGitHubWebhookHttp(
        configuration=Configuration(secret, target),
        publisher=publisher,
        config=load_http_config(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(handler=handler)),
        base_url="http://fixture.invalid",
    ) as client:
        response = await client.post("/v1/github/webhook", content=BODY)
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "github webhook ingress is not configured",
    }
    assert publisher.records == []


async def test_size_limit_precedes_bad_signature(client, publisher):
    body = b"x" * (load_http_config().max_body_bytes + 1)
    response = await client.post("/v1/github/webhook", content=body, headers=headers())
    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "delivery body too large"}
    assert publisher.records == []


async def test_publish_failure_returns_retryable_response(client, publisher):
    publisher.fail = True
    response = await client.post("/v1/github/webhook", content=BODY, headers=headers())
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "delivery not published"}


async def test_202_waits_for_publish_acknowledgement(client, publisher):
    publisher.release.clear()
    task = asyncio.create_task(
        client.post("/v1/github/webhook", content=BODY, headers=headers())
    )
    try:
        await asyncio.wait_for(publisher.entered.wait(), timeout=2)
        assert not task.done()
        publisher.release.set()
        assert (await task).status_code == 202
    finally:
        publisher.release.set()
        await task


async def test_body_is_not_parsed_at_http_boundary(client, publisher):
    body = b"signed arbitrary bytes\xff"
    response = await client.post(
        "/v1/github/webhook", content=body, headers=headers(body)
    )
    assert response.status_code == 202
    assert base64.b64decode(publisher.records[0][2]["payload"]["body_b64"]) == body


async def test_redelivery_reuses_delivery_correlation(client, publisher):
    for _ in range(2):
        assert (
            await client.post("/v1/github/webhook", content=BODY, headers=headers())
        ).status_code == 202
    assert [r[1] for r in publisher.records] == [DELIVERY.encode()] * 2
    assert [r[2]["correlation_id"] for r in publisher.records] == [DELIVERY] * 2


async def test_publish_timeout_is_retryable(client, publisher):
    config = load_http_config().model_copy(update={"publish_timeout_seconds": 0.01})
    handler = HandlerGitHubWebhookHttp(
        configuration=Configuration(), publisher=publisher, config=config
    )
    publisher.release.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(handler=handler)),
        base_url="http://fixture.invalid",
    ) as client:
        response = await client.post(config.path, content=BODY, headers=headers())
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "delivery not published"}
    assert publisher.records == []


async def test_chunked_body_obeys_the_limit_without_content_length(client, publisher):
    async def chunks():
        yield b"x" * load_http_config().max_body_bytes
        yield b"overflow"
        raise AssertionError("oversized stream must not be read further")

    response = await client.post(
        "/v1/github/webhook", content=chunks(), headers=headers()
    )
    assert response.status_code == 413
    assert publisher.records == []


async def test_exact_body_limit_is_accepted(client, publisher):
    body = b"x" * load_http_config().max_body_bytes
    response = await client.post(
        "/v1/github/webhook", content=body, headers=headers(body)
    )
    assert response.status_code == 202
    assert len(publisher.records) == 1


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/github/webhook", 405),
        ("POST", "/v1/github/other", 404),
        ("POST", "/v1/github/webhook/other", 404),
    ],
)
async def test_mount_exposes_only_the_declared_route(
    client, publisher, method, path, status
):
    response = await client.request(method, path, content=BODY, headers=headers())
    assert response.status_code == status
    assert publisher.records == []


async def test_environment_overlay_fails_closed_without_target(monkeypatch):
    config = load_http_config()
    for name in config.environment.values():
        monkeypatch.delenv(name, raising=False)
    assert await EnvironmentWebhookConfiguration(config).resolve() == (b"", None)
    monkeypatch.setenv(config.environment["secret"], SIGNING_KEY.decode())
    monkeypatch.setenv(config.environment["tenant_id"], "not-a-uuid")
    monkeypatch.setenv(config.environment["tenant_slug"], "fixture-tenant")
    monkeypatch.setenv(config.environment["principal_id"], "fixture-principal")
    secret, target = await EnvironmentWebhookConfiguration(config).resolve()
    assert secret == SIGNING_KEY
    assert target is None


async def test_real_inmemory_bus_receives_the_command():
    bus = EventBusInmemory(environment="test", group="webhook-http-test")
    await bus.start()
    received = []

    async def receive(message):
        received.append(json.loads(message.value))

    config = load_http_config()
    unsubscribe = await bus.subscribe(
        config.delivery_topic, group_id="webhook-http-test", on_message=receive
    )
    handler = HandlerGitHubWebhookHttp(
        configuration=Configuration(), publisher=BusWebhookPublisher(bus), config=config
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(handler=handler)),
            base_url="http://fixture.invalid",
        ) as client:
            response = await client.post(config.path, content=BODY, headers=headers())
        assert response.status_code == 202
        assert received[0]["payload"]["delivery_id"] == DELIVERY
    finally:
        await unsubscribe()
        await bus.close()


def test_contract_mount_and_topic_match_existing_runtime_consumer():
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_github_webhook_ingress_effect/contract.yaml"
        ).read_text()
    )
    assert contract["http_mount"]["path_prefix"] == "/v1/github"
    assert (
        contract["http_ingress"]["delivery_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["http_mount"]["factory"].endswith("handlers.http_app:create_app")
    assert contract["http_ingress"]["handler"] == {
        "name": HandlerGitHubWebhookHttp.__name__,
        "module": HandlerGitHubWebhookHttp.__module__,
    }


def test_factory_refuses_a_different_contract_handler(monkeypatch):
    config = load_http_config()
    wrong_handler = config.handler.model_copy(update={"name": "OtherHandler"})
    monkeypatch.setattr(
        http_app,
        "load_http_config",
        lambda: config.model_copy(update={"handler": wrong_handler}),
    )
    with pytest.raises(ValueError, match="HTTP handler binding"):
        create_app()
