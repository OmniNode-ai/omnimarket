# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity node binds a dev user to an EXISTING tenant (OMN-26476).

Given a tenant id the run reads that tenant over the bus (the runtime's tenant read command), creates the named user,
sets the user's tenant attributes (the ones the sign-in client's mappers turn into the tenant claims), signs in and
reads the claims back out of the token it got: the audience and the two tenant claims the inference-credentials route's
identity resolver checks. It never asks the runtime to provision a tenant in this mode. Every name (attributes, claims,
audience, topics) comes from the contract; the seams below check them against the repo's own edge resolver and the
sign-in client overlay.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.handlers.edge_bearer_jwt_identity import BearerJwtTenantIdentity
from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    BusTenantReader,
)
from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityCleanup,
    EnumDevIdentityOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_binding import (
    ModelTenantBinding,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantReadAnswer,
    ModelTenantReadRequest,
    ModelTenantReadTopics,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols import (
    TenantProvisionUnavailableError,
)
from omnimarket.nodes.node_dev_identity_provision_effect.runtime_dev_identity_provision import (
    load_tenant_binding,
    load_tenant_read_topics,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantReadOutcome,
    ModelTenantReadCommand,
    ModelTenantReadResult,
)

from .fakes import (
    FakeKeycloakAndApi,
    FakeKeyGenerator,
    FakeSettingStore,
    FakeTenantProvisioner,
)
from .test_golden_chain_dev_identity_provision import CONTRACT_PATH, _overlay, _seed

pytestmark = pytest.mark.unit

TENANT_ID = UUID("8044e4eb-0e41-4f94-ad61-ab63064f4198")
TENANT_SLUG = "cli-tenant-a"
USERNAME = "cli-run-a"
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEYCLOAK_OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect/overlay.example.yaml"
)


class FakeBindKeycloak(FakeKeycloakAndApi):
    """The strict Keycloak fake plus user attributes and signed access tokens built from them the way mappers do.

    ``audience`` is what the client's audience mapper stamps (None: no such mapper); ``keep_attributes`` False models a
    realm whose user profile drops unmanaged attributes; ``fail_attribute_write`` refuses the user update.
    """

    def __init__(
        self,
        *,
        audience: str | None,
        mappers: dict[str, str],
        keep_attributes: bool = True,
        fail_attribute_write: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(username=USERNAME, **kwargs)
        self.audience = audience
        self.mappers = mappers
        self.keep_attributes = keep_attributes
        self.fail_attribute_write = fail_attribute_write
        self.attributes: dict[str, dict[str, list[str]]] = {}
        self.put_bodies: list[dict[str, Any]] = []

    def _user_for(self, url: str) -> dict[str, Any] | None:
        user_id = url.rstrip("/").rsplit("/", 1)[-1]
        return next((u for u in self.users.values() if u["id"] == user_id), None)

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse:
        is_user_url = (
            "/admin/realms/" in request.url
            and "/users/" in request.url
            and "/reset-password" not in request.url
        )
        if is_user_url and request.method in ("GET", "PUT"):
            self.call_log.append(f"{request.method} {request.url}")
            self.sent.append(request)
            if not self._is_admin(request):
                return self._answer(403)
            user = self._user_for(request.url)
            if user is None:
                return self._answer(404)
            if request.method == "GET":
                return self._answer(
                    200, {**user, "attributes": self.attributes.get(user["id"], {})}
                )
            if self.fail_attribute_write:
                return self._answer(403)
            body = dict(request.json_body or {})
            self.put_bodies.append(body)
            if self.keep_attributes:
                self.attributes[user["id"]] = dict(body.get("attributes", {}))
            return self._answer(204)
        response = await super().send(request)
        form = {k: v.get_secret_value() for k, v in (request.form or {}).items()}
        if form.get("grant_type") == "password" and response.status == 200:
            return self._answer(200, {"access_token": self._signed_token(form)})
        return response

    def _signed_token(self, form: dict[str, str]) -> str:
        user_id = self.users[form["username"]]["id"]
        claims: dict[str, Any] = {
            "iss": "https://issuer.invalid/realms/dev",
            "exp": int(time.time()) + 300,
            "sub": user_id,
        }
        if self.audience is not None:
            claims["aud"] = [self.audience, "account"]
        for attribute, claim in self.mappers.items():
            values = self.attributes.get(user_id, {}).get(attribute)
            if values:
                claims[claim] = values[0]
        return jwt.encode(claims, SIGNER, algorithm="RS256", headers={"kid": "k1"})


class FakeTenantReader:
    """The runtime's tenant read command in memory."""

    def __init__(
        self,
        *,
        outcome: str = "found",
        tenant_slug: str | None = TENANT_SLUG,
        provisioning_state: str | None = "active",
        unavailable: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.tenant_slug = tenant_slug
        self.provisioning_state = provisioning_state
        self.unavailable = unavailable
        self.requests: list[ModelTenantReadRequest] = []

    async def read(self, request: ModelTenantReadRequest) -> ModelTenantReadAnswer:
        self.requests.append(request)
        if self.unavailable is not None:
            raise TenantProvisionUnavailableError(self.unavailable)
        found = self.outcome == "found"
        return ModelTenantReadAnswer(
            command_id=request.command_id,
            outcome=self.outcome,
            detail="fake runtime answer",
            tenant_id=str(request.tenant_id) if found else None,
            tenant_slug=self.tenant_slug if found else None,
            provisioning_state=self.provisioning_state if found else None,
        )


def _binding() -> ModelTenantBinding:
    return load_tenant_binding(CONTRACT_PATH)


def _keycloak(**overrides: Any) -> FakeBindKeycloak:
    binding = _binding()
    fields: dict[str, Any] = {
        "audience": binding.audience,
        "mappers": {
            binding.tenant_id_attribute: binding.tenant_id_claim,
            binding.tenant_slug_attribute: binding.tenant_slug_claim,
        },
    }
    fields.update(overrides)
    return FakeBindKeycloak(**fields)


def _rig(
    transport: FakeKeycloakAndApi,
    reader: FakeTenantReader,
    provisioner: FakeTenantProvisioner,
    *,
    binding: ModelTenantBinding | None = None,
    with_reader: bool = True,
) -> HandlerDevIdentityProvision:
    store = FakeSettingStore()
    _seed(store)
    # The named user's own generated key, where the fake generator stores it (folder /shared/env, the id upper-cased).
    store.set("/shared/env", "CLI_RUN_A_PASSWORD", "Dp1!generated")
    return HandlerDevIdentityProvision(
        overlay=_overlay(),
        transport=transport,
        settings_store=store,
        key_generator=FakeKeyGenerator(outcome="created"),
        tenant_provisioner=provisioner,
        tenant_reader=reader if with_reader else None,
        tenant_binding=binding,
        contract_path=CONTRACT_PATH,
    )


def _bind(handler: HandlerDevIdentityProvision, **fields: Any) -> Any:
    command = ModelDevIdentityProvisionCommand(
        lane="dev",
        write=True,
        username=USERNAME,
        email="cli-run-a@example.invalid",
        password_key_id="cli_run_a_password",
        tenant_id=TENANT_ID,
        **fields,
    )
    return asyncio.run(handler.handle(command))


def _bound(
    **keycloak: Any,
) -> tuple[Any, FakeBindKeycloak, FakeTenantReader, FakeTenantProvisioner]:
    transport = _keycloak(**keycloak)
    reader = FakeTenantReader()
    provisioner = FakeTenantProvisioner()
    result = _bind(_rig(transport, reader, provisioner, binding=_binding()))
    return result, transport, reader, provisioner


# --- the happy path: bound to the existing tenant, claims read back ---------------------------------------------------


def test_a_user_is_bound_to_the_existing_tenant_and_the_token_carries_the_claims() -> (
    None
):
    result, transport, reader, _ = _bound()
    binding = _binding()

    assert result.ok is True
    assert result.outcome is EnumDevIdentityOutcome.BOUND
    assert result.tenant_id == str(TENANT_ID)
    assert result.tenant_slug == TENANT_SLUG
    assert result.user_name == USERNAME
    assert result.token_audience == binding.audience
    assert result.claims_verified == [
        "aud",
        binding.tenant_id_claim,
        binding.tenant_slug_claim,
    ]
    assert [r.tenant_id for r in reader.requests] == [TENANT_ID]
    # The attributes the mappers read were written on the user, by the contract's names.
    (put,) = transport.put_bodies
    assert put["attributes"][binding.tenant_id_attribute] == [str(TENANT_ID)]
    assert put["attributes"][binding.tenant_slug_attribute] == [TENANT_SLUG]
    assert list(transport.users) == [USERNAME]


def test_binding_never_asks_the_runtime_to_provision_a_tenant() -> None:
    _, _, reader, provisioner = _bound()
    assert provisioner.requests == []
    assert len(reader.requests) == 1


def test_the_token_the_node_accepts_is_the_token_the_edge_resolver_accepts() -> None:
    """Seam: a token with the claims the node verified resolves, through the repo's own edge resolver, to the tenant."""
    binding = _binding()
    transport = _keycloak()
    _bind(_rig(transport, FakeTenantReader(), FakeTenantProvisioner(), binding=binding))
    token = transport._signed_token({"username": USERNAME})

    class _Keys:
        def signing_key_for(self, token: str) -> Any:
            return SIGNER.public_key()

    resolver = BearerJwtTenantIdentity(
        ModelEdgeIdentityOverlay(
            jwks_url="https://issuer.invalid/certs",
            issuer="https://issuer.invalid/realms/dev",
            audience=binding.audience,
            algorithms=("RS256",),
            tenant_id_claim=binding.tenant_id_claim,
            tenant_slug_claim=binding.tenant_slug_claim,
            principal_id_claim="sub",
            jwks_cache_seconds=300,
            leeway_seconds=30,
        ),
        _Keys(),
    )
    resolved = asyncio.run(resolver.resolve({"authorization": f"Bearer {token}"}))
    assert resolved == ModelHttpTenantIdentity(
        tenant_id=TENANT_ID,
        tenant_slug=TENANT_SLUG,
        principal_id=transport.users[USERNAME]["id"],
    )


def test_the_result_carries_no_token_password_or_secret() -> None:
    result, transport, _, _ = _bound()
    token = transport._signed_token({"username": USERNAME})
    for text in (result.model_dump_json(), repr(result), str(result)):
        assert token not in text
        assert "Dp1!generated" not in text
        assert "admin-secret-value" not in text


# --- refusals before any write ---------------------------------------------------------------------------------------


def test_a_tenant_the_runtime_does_not_know_is_refused_and_no_user_is_made() -> None:
    transport = _keycloak()
    handler = _rig(
        transport,
        FakeTenantReader(outcome="not_found"),
        FakeTenantProvisioner(),
        binding=_binding(),
    )
    result = _bind(handler)
    assert result.outcome is EnumDevIdentityOutcome.TENANT_NOT_FOUND
    assert result.ok is False
    assert transport.sent == []


def test_a_tenant_that_is_not_active_is_refused_and_no_user_is_made() -> None:
    transport = _keycloak()
    handler = _rig(
        transport,
        FakeTenantReader(provisioning_state="suspended"),
        FakeTenantProvisioner(),
        binding=_binding(),
    )
    result = _bind(handler)
    assert result.outcome is EnumDevIdentityOutcome.TENANT_NOT_ACTIVE
    assert transport.sent == []


def test_a_runtime_that_does_not_answer_is_a_typed_failure_and_no_user_is_made() -> (
    None
):
    transport = _keycloak()
    handler = _rig(
        transport,
        FakeTenantReader(unavailable="no answer within 30s"),
        FakeTenantProvisioner(),
        binding=_binding(),
    )
    result = _bind(handler)
    assert result.outcome is EnumDevIdentityOutcome.RUNTIME_UNREACHABLE
    assert transport.sent == []


def test_with_no_binding_declared_the_run_is_refused_before_any_call() -> None:
    transport = _keycloak()
    reader = FakeTenantReader()
    handler = _rig(transport, reader, FakeTenantProvisioner(), binding=None)
    result = _bind(handler)
    assert result.outcome is EnumDevIdentityOutcome.TENANT_BINDING_UNCONFIGURED
    assert transport.sent == []
    assert reader.requests == []


def test_with_no_tenant_reader_the_run_is_refused_before_any_call() -> None:
    transport = _keycloak()
    handler = _rig(
        transport,
        FakeTenantReader(),
        FakeTenantProvisioner(),
        binding=_binding(),
        with_reader=False,
    )
    result = _bind(handler)
    assert result.outcome is EnumDevIdentityOutcome.TENANT_BINDING_UNCONFIGURED
    assert transport.sent == []


def test_a_lane_that_is_not_dev_is_refused_even_with_a_tenant_id() -> None:
    transport = _keycloak()
    reader = FakeTenantReader()
    handler = _rig(transport, reader, FakeTenantProvisioner(), binding=_binding())
    command = ModelDevIdentityProvisionCommand(
        lane="prod",
        write=True,
        username=USERNAME,
        email="x@example.invalid",
        password_key_id="x",
        tenant_id=TENANT_ID,
    )
    result = asyncio.run(handler.handle(command))
    assert result.outcome is EnumDevIdentityOutcome.REFUSED_NOT_DEV
    assert transport.sent == []
    assert reader.requests == []


def test_an_existing_user_is_still_refused_without_reset_existing() -> None:
    transport = _keycloak()
    handler = _rig(
        transport, FakeTenantReader(), FakeTenantProvisioner(), binding=_binding()
    )
    first = _bind(handler)
    assert first.ok is True
    second = _bind(handler)
    assert second.outcome is EnumDevIdentityOutcome.USER_EXISTS_REFUSED
    assert len(transport.put_bodies) == 1


def test_an_existing_user_is_bound_and_kept_with_reset_existing() -> None:
    transport = _keycloak()
    handler = _rig(
        transport, FakeTenantReader(), FakeTenantProvisioner(), binding=_binding()
    )
    _bind(handler)
    again = _bind(handler, reset_existing=True)
    assert again.outcome is EnumDevIdentityOutcome.BOUND
    assert again.cleanup is EnumDevIdentityCleanup.NOT_CREATED
    assert USERNAME in transport.users


# --- the read-back refuses a token that would not pass the route ------------------------------------------------------


def test_a_client_with_no_audience_mapper_fails_the_readback_and_the_new_user_is_deleted() -> (
    None
):
    result, transport, _, _ = _bound(audience=None)
    assert result.outcome is EnumDevIdentityOutcome.TOKEN_CLAIMS_MISMATCH
    assert result.ok is False
    assert "aud" in result.detail
    assert result.cleanup is EnumDevIdentityCleanup.DELETED
    assert USERNAME not in transport.users


def test_a_wrong_audience_fails_the_readback() -> None:
    result, _, _, _ = _bound(audience="some-other-audience")
    assert result.outcome is EnumDevIdentityOutcome.TOKEN_CLAIMS_MISMATCH
    assert "aud" in result.detail


def test_a_realm_that_drops_the_attributes_fails_the_readback_naming_both_claims() -> (
    None
):
    binding = _binding()
    result, _, _, _ = _bound(keep_attributes=False)
    assert result.outcome is EnumDevIdentityOutcome.TOKEN_CLAIMS_MISMATCH
    assert binding.tenant_id_claim in result.detail
    assert binding.tenant_slug_claim in result.detail
    assert result.cleanup is EnumDevIdentityCleanup.DELETED


def test_a_token_naming_another_tenant_fails_the_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    transport = _keycloak()
    original = transport._signed_token

    def other_tenant(form: dict[str, str]) -> str:
        user_id = transport.users[form["username"]]["id"]
        transport.attributes[user_id][binding.tenant_id_attribute] = [str(uuid4())]
        return original(form)

    monkeypatch.setattr(transport, "_signed_token", other_tenant)
    result = _bind(
        _rig(transport, FakeTenantReader(), FakeTenantProvisioner(), binding=binding)
    )
    assert result.outcome is EnumDevIdentityOutcome.TOKEN_CLAIMS_MISMATCH
    assert binding.tenant_id_claim in result.detail


def test_a_refused_attribute_write_is_typed_and_the_new_user_is_deleted() -> None:
    result, transport, _, _ = _bound(fail_attribute_write=True)
    assert result.outcome is EnumDevIdentityOutcome.USER_ATTRIBUTES_FAILED
    assert result.cleanup is EnumDevIdentityCleanup.DELETED
    assert USERNAME not in transport.users


# --- the command ----------------------------------------------------------------------------------------------------


def test_a_tenant_id_needs_a_username_and_a_write() -> None:
    with pytest.raises(ValidationError, match="username"):
        ModelDevIdentityProvisionCommand(lane="dev", write=True, tenant_id=TENANT_ID)
    with pytest.raises(ValidationError, match="write"):
        ModelDevIdentityProvisionCommand(
            lane="dev", write=False, username=USERNAME, tenant_id=TENANT_ID
        )


def test_a_tenant_id_excludes_the_lookup_and_delete_verbs() -> None:
    with pytest.raises(ValidationError, match="tenant_id"):
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=True,
            username=USERNAME,
            tenant_id=TENANT_ID,
            delete_user=True,
        )
    with pytest.raises(ValidationError, match="tenant_id"):
        ModelDevIdentityProvisionCommand(
            lane="dev",
            write=False,
            username=USERNAME,
            tenant_id=TENANT_ID,
            lookup_user=True,
        )


def test_a_tenant_id_must_be_a_uuid() -> None:
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionCommand.model_validate(
            {"lane": "dev", "write": True, "username": "x", "tenant_id": "not-a-uuid"}
        )


# --- the bus adapter, on an in-memory bus -----------------------------------------------------------------------------

TOPICS = ModelTenantReadTopics(
    command_topic="onex.cmd.omnimarket.tenant-read-requested.v1",
    completed_topic="onex.evt.omnimarket.tenant-read-completed.v1",
    wait_seconds=0.5,
)


def _opener(bus: EventBusInmemory) -> Any:
    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[EventBusInmemory]:
        yield bus

    return open_bus


def _read_answer(command_id: UUID, tenant_id: UUID) -> bytes:
    from datetime import UTC, datetime

    result = ModelTenantReadResult(
        command_id=command_id,
        outcome=EnumTenantReadOutcome.FOUND,
        detail="tenant found",
        tenant_id=tenant_id,
        tenant_slug=TENANT_SLUG,
        name="n",
        provisioning_state="active",
        plan_code="free",
        created_at=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
    )
    return json.dumps({"payload": json.loads(result.model_dump_json())}).encode()


@pytest.mark.asyncio
async def test_the_reader_sends_the_read_envelope_and_resolves_from_its_own_answer() -> (
    None
):
    bus = EventBusInmemory(environment="test", group="dev-identity")
    await bus.start()
    seen: list[dict[str, Any]] = []

    async def on_command(message: Any) -> None:
        raw = json.loads(message.value)
        seen.append(raw)
        command = ModelTenantReadCommand.model_validate(raw["payload"])
        assert command.tenant_id == TENANT_ID
        assert command.tenant_slug is None
        await bus.publish(
            TOPICS.completed_topic, None, _read_answer(uuid4(), TENANT_ID)
        )
        await bus.publish(
            TOPICS.completed_topic,
            None,
            _read_answer(command.command_id, command.tenant_id or TENANT_ID),
        )

    try:
        await bus.subscribe_ungrouped(TOPICS.command_topic, on_command)
        reader = BusTenantReader(open_bus=_opener(bus), topics=TOPICS)
        request = ModelTenantReadRequest(command_id=uuid4(), tenant_id=TENANT_ID)
        answer = await reader.read(request)
    finally:
        await bus.close()
    assert answer.command_id == request.command_id
    assert answer.outcome == "found"
    assert answer.tenant_slug == TENANT_SLUG
    assert answer.provisioning_state == "active"
    assert len(seen) == 1
    assert seen[0]["event_type"] == "omnimarket.tenant-read-requested"


@pytest.mark.asyncio
async def test_no_read_answer_inside_the_wait_is_unavailable() -> None:
    bus = EventBusInmemory(environment="test", group="dev-identity")
    await bus.start()
    try:
        reader = BusTenantReader(open_bus=_opener(bus), topics=TOPICS)
        with pytest.raises(TenantProvisionUnavailableError, match="no answer"):
            await reader.read(
                ModelTenantReadRequest(command_id=uuid4(), tenant_id=TENANT_ID)
            )
    finally:
        await bus.close()


# --- the seams -------------------------------------------------------------------------------------------------------


def _contract(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def test_the_declared_read_topics_are_the_tenant_provision_nodes_own() -> None:
    from importlib import resources

    tenant = yaml.safe_load(
        resources.files("omnimarket.nodes.node_tenant_provision_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )["topics"]["tenant_read"]
    declared = load_tenant_read_topics(CONTRACT_PATH)
    assert declared.command_topic == tenant["requested"]
    assert declared.completed_topic == tenant["completed"]
    assert declared.wait_seconds > 0


def test_the_read_request_is_a_command_the_tenant_node_accepts_and_its_answer_is_readable() -> (
    None
):
    request = ModelTenantReadRequest(command_id=uuid4(), tenant_id=TENANT_ID)
    command = ModelTenantReadCommand.model_validate_json(request.model_dump_json())
    assert (command.command_id, command.tenant_id) == (
        request.command_id,
        TENANT_ID,
    )
    from datetime import UTC, datetime

    result = ModelTenantReadResult(
        command_id=request.command_id,
        outcome=EnumTenantReadOutcome.FOUND,
        detail="tenant found",
        tenant_id=TENANT_ID,
        tenant_slug=TENANT_SLUG,
        name="n",
        provisioning_state="active",
        plan_code="free",
        created_at=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
    )
    answer = ModelTenantReadAnswer.model_validate_json(result.model_dump_json())
    assert answer.outcome == EnumTenantReadOutcome.FOUND.value
    assert answer.tenant_id == str(TENANT_ID)
    assert answer.tenant_slug == TENANT_SLUG
    assert answer.provisioning_state == "active"


def test_the_binding_names_are_the_sign_in_clients_mapper_names() -> None:
    """Seam: the user attributes and claim names the node sets are the ones the sign-in client's mappers carry."""
    binding = _binding()
    overlay = _contract(KEYCLOAK_OVERLAY_EXAMPLE)
    pairs = {
        (m["config"]["user.attribute"], m["config"]["claim.name"])
        for m in overlay["sign_in_client"]["mappers"]
    }
    assert (binding.tenant_id_attribute, binding.tenant_id_claim) in pairs
    assert (binding.tenant_slug_attribute, binding.tenant_slug_claim) in pairs


def test_the_binding_is_declared_in_the_contract_with_every_name() -> None:
    declared = _contract(CONTRACT_PATH)["tenant_binding"]
    assert set(declared) == set(ModelTenantBinding.model_fields)
    assert all(isinstance(v, str) and v for v in declared.values())


def test_a_binding_missing_a_name_is_refused() -> None:
    declared = dict(_contract(CONTRACT_PATH)["tenant_binding"])
    del declared["audience"]
    with pytest.raises(ValidationError):
        ModelTenantBinding.model_validate(declared)


def test_the_encoded_token_helper_reads_only_the_payload() -> None:
    """Failing control for the read-back: a string that is not a three-part token yields no claims."""
    from omnimarket.nodes.node_dev_identity_provision_effect.handlers.token_claims import (
        read_token_claims,
    )

    payload = base64.urlsafe_b64encode(json.dumps({"a": 1}).encode()).rstrip(b"=")
    assert read_token_claims(f"h.{payload.decode()}.s") == {"a": 1}
    assert read_token_claims("not-a-token") is None
    assert read_token_claims("a.%%%.c") is None
