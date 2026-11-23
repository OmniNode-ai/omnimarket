# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity node creates its probe tenant through the runtime, not through a separate HTTP API (OMN-23563).

The separate onex-api is gone from the dev lane, so a node that posts to its provision route can never provision again.
The tenant is asked of the runtime's tenant provision command over the bus: the handler hands a typed request to the
tenant provisioner seam, the bus adapter sends it as an envelope on the declared command topic and resolves from the
answer carrying its own command id, and the topics and the two wire shapes are checked against the tenant provision
node's own contract and models.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    BusTenantProvisioner,
)
from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionAnswer,
    ModelTenantProvisionRequest,
    ModelTenantProvisionTopics,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols import (
    TenantProvisionUnavailableError,
)
from omnimarket.nodes.node_dev_identity_provision_effect.runtime_dev_identity_provision import (
    load_tenant_provision_topics,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantProvisionOutcome,
    ModelTenantProvisionCommand,
    ModelTenantProvisionResult,
)

from .fakes import (
    FakeKeycloakAndApi,
    FakeKeyGenerator,
    FakeSettingStore,
    FakeTenantProvisioner,
)
from .test_golden_chain_dev_identity_provision import CONTRACT_PATH, _overlay, _seed

pytestmark = pytest.mark.unit

TENANT_CONTRACT_NODE = "omnimarket.nodes.node_tenant_provision_effect"


def _handler(
    transport: FakeKeycloakAndApi,
    provisioner: FakeTenantProvisioner,
    overlay: ModelDevIdentityProvisionOverlay | None = None,
) -> HandlerDevIdentityProvision:
    store = FakeSettingStore()
    _seed(store)
    store.set("/shared/env", "OTHER_USER_PASSWORD", "Dp1!generated")
    return HandlerDevIdentityProvision(
        overlay=overlay or _overlay(),
        transport=transport,
        settings_store=store,
        key_generator=FakeKeyGenerator(outcome="created"),
        tenant_provisioner=provisioner,
        contract_path=CONTRACT_PATH,
    )


def _run(handler: HandlerDevIdentityProvision, **fields: Any) -> Any:
    command = ModelDevIdentityProvisionCommand(lane="dev", write=True, **fields)
    return asyncio.run(handler.handle(command))


def test_a_run_makes_no_call_to_a_separate_api_and_asks_the_runtime_once() -> None:
    transport = FakeKeycloakAndApi()
    provisioner = FakeTenantProvisioner()

    result = _run(_handler(transport, provisioner))

    assert result.outcome.value == "provisioned"
    assert [r.url for r in transport.sent if "/provision" in r.url] == []
    assert len(provisioner.requests) == 1
    assert provisioner.requests[0].tenant_slug == "dev-probe-user"
    assert provisioner.requests[0].name == "dev-probe-user"


def test_the_command_id_is_the_same_for_the_same_user_and_differs_between_users() -> (
    None
):
    first, again, other = (
        FakeTenantProvisioner(),
        FakeTenantProvisioner(),
        FakeTenantProvisioner(),
    )
    _run(_handler(FakeKeycloakAndApi(), first))
    _run(_handler(FakeKeycloakAndApi(), again))
    _run(
        _handler(
            FakeKeycloakAndApi(username="Other_User.7", password="Dp1!generated"),
            other,
        ),
        username="Other_User.7",
        email="other@example.invalid",
        password_key_id="other_user_password",
    )
    assert first.requests[0].command_id == again.requests[0].command_id
    assert other.requests[0].command_id != first.requests[0].command_id
    assert other.requests[0].tenant_slug == "other-user-7"


def test_a_username_with_nothing_to_make_a_slug_of_is_refused_before_any_write() -> (
    None
):
    transport = FakeKeycloakAndApi(username="___")
    provisioner = FakeTenantProvisioner()

    result = _run(
        _handler(transport, provisioner),
        username="___",
        email="x@example.invalid",
        password_key_id="x_password",
    )

    assert result.outcome.value == "invalid_identity_input"
    assert transport.sent == []
    assert provisioner.requests == []


def test_an_already_existing_tenant_is_a_provisioned_probe() -> None:
    result = _run(
        _handler(FakeKeycloakAndApi(), FakeTenantProvisioner(outcome="already_exists"))
    )
    assert result.outcome.value == "provisioned"
    assert result.tenant_id == "tenant-abc-123"


def test_the_retired_api_block_of_a_deployed_overlay_still_loads_and_is_never_read() -> (
    None
):
    document = _overlay().model_dump(mode="json")
    document["api"] = {
        "base_url": "https://api.example.invalid",
        "provision_path": "/v1/auth/provision",
        "timeout_seconds": 15.0,
    }
    legacy = ModelDevIdentityProvisionOverlay.model_validate(document)
    assert legacy.api is not None
    transport = FakeKeycloakAndApi()
    result = _run(_handler(transport, FakeTenantProvisioner(), legacy))
    assert result.outcome.value == "provisioned"
    assert not any("api.example.invalid" in r.url for r in transport.sent)


# --- the bus adapter, on an in-memory bus ---------------------------------------------------------------------------

TOPICS = ModelTenantProvisionTopics(
    command_topic="onex.cmd.omnimarket.tenant-provision-requested.v1",
    completed_topic="onex.evt.omnimarket.tenant-provision-completed.v1",
    wait_seconds=0.5,
)


def _request() -> ModelTenantProvisionRequest:
    return ModelTenantProvisionRequest(
        command_id=uuid4(), tenant_slug="dev-probe-user", name="dev-probe-user"
    )


def _opener(bus: EventBusInmemory) -> Any:
    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[EventBusInmemory]:
        yield bus

    return open_bus


def _answer_bytes(command_id: UUID, *, wrapped: bool) -> bytes:
    result = ModelTenantProvisionResult(
        command_id=command_id,
        outcome=EnumTenantProvisionOutcome.CREATED,
        detail="tenant created",
        tenant_id=uuid4(),
        tenant_slug="dev-probe-user",
        name="dev-probe-user",
        provisioning_state="active",
        plan_code="free",
        created_at=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
    )
    payload = json.loads(result.model_dump_json())
    body = (
        {"payload": payload, "parent_envelope_id": str(uuid4())} if wrapped else payload
    )
    return json.dumps(body).encode()


async def _serve(
    bus: EventBusInmemory,
    seen: list[dict[str, Any]],
    *,
    wrapped: bool,
    noise_first: bool = False,
) -> None:
    async def on_command(message: Any) -> None:
        raw = json.loads(message.value)
        seen.append(raw)
        command = ModelTenantProvisionCommand.model_validate(raw["payload"])
        if noise_first:
            await bus.publish(
                TOPICS.completed_topic,
                None,
                _answer_bytes(uuid4(), wrapped=wrapped),
            )
        await bus.publish(
            TOPICS.completed_topic,
            None,
            _answer_bytes(command.command_id, wrapped=wrapped),
        )

    await bus.subscribe_ungrouped(TOPICS.command_topic, on_command)


async def _provision(
    request: ModelTenantProvisionRequest, **serve: bool
) -> tuple[ModelTenantProvisionAnswer, list[dict[str, Any]]]:
    bus = EventBusInmemory(environment="test", group="dev-identity")
    await bus.start()
    seen: list[dict[str, Any]] = []
    try:
        await _serve(bus, seen, **serve)
        provisioner = BusTenantProvisioner(open_bus=_opener(bus), topics=TOPICS)
        return await provisioner.provision(request), seen
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
async def test_the_adapter_sends_the_envelope_and_resolves_from_its_own_answer(
    wrapped: bool,
) -> None:
    request = _request()
    answer, seen = await _provision(request, wrapped=wrapped, noise_first=True)
    assert answer.command_id == request.command_id
    assert answer.outcome == "created"
    assert answer.tenant_id is not None
    assert len(seen) == 1
    assert seen[0]["event_type"] == "omnimarket.tenant-provision-requested"
    assert seen[0]["payload"]["tenant_slug"] == "dev-probe-user"


@pytest.mark.asyncio
async def test_no_answer_inside_the_wait_is_runtime_unavailable() -> None:
    bus = EventBusInmemory(environment="test", group="dev-identity")
    await bus.start()
    try:
        provisioner = BusTenantProvisioner(open_bus=_opener(bus), topics=TOPICS)
        with pytest.raises(TenantProvisionUnavailableError, match="no answer"):
            await provisioner.provision(_request())
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_a_bus_that_cannot_be_opened_is_runtime_unavailable() -> None:
    from omnimarket.handlers.bus_connect.errors import LabRunBusError

    @contextlib.asynccontextmanager
    async def refuses() -> AsyncIterator[Any]:
        raise LabRunBusError("no broker address was resolved")
        yield

    provisioner = BusTenantProvisioner(open_bus=refuses, topics=TOPICS)
    with pytest.raises(TenantProvisionUnavailableError, match="no broker address"):
        await provisioner.provision(_request())


# --- the seams with the tenant provision node ------------------------------------------------------------------------


def _tenant_contract() -> dict[str, Any]:
    text = (
        resources.files(TENANT_CONTRACT_NODE)
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_declared_topics_are_the_tenant_provision_nodes_own() -> None:
    declared = load_tenant_provision_topics()
    tenant = _tenant_contract()["topics"]["tenant_provision"]
    assert declared.command_topic == tenant["requested"]
    assert declared.completed_topic == tenant["completed"]
    assert declared.wait_seconds > 0


def test_the_request_is_a_command_the_tenant_provision_node_accepts() -> None:
    request = _request()
    command = ModelTenantProvisionCommand.model_validate_json(request.model_dump_json())
    assert command.command_id == request.command_id
    assert command.tenant_slug == request.tenant_slug
    assert command.carries_owner() is False


def test_the_tenant_provision_nodes_answer_is_readable_as_the_answer() -> None:
    result = ModelTenantProvisionResult(
        command_id=uuid4(),
        outcome=EnumTenantProvisionOutcome.ALREADY_EXISTS,
        detail="tenant already exists for this owner; nothing written",
        tenant_id=uuid4(),
        tenant_slug="dev-probe-user",
        name="dev-probe-user",
        provisioning_state="active",
        plan_code="free",
        created_at=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
    )
    answer = ModelTenantProvisionAnswer.model_validate_json(result.model_dump_json())
    assert answer.outcome == result.outcome.value
    assert answer.tenant_id == str(result.tenant_id)
    assert answer.tenant_slug == result.tenant_slug
