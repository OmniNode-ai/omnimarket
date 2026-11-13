# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-only guard refuses any lane, realm or role the contract does not declare (OMN-24237).

A non-dev lane in the command, a non-dev lane in the overlay, a realm the contract does not declare
and a role outside the contract's allowed roles are each refused with zero calls to the credential
source, the transport, the store and the generator: ``refused_not_dev`` for a lane or a realm,
``refused_role_not_allowed`` for a role. The failing control writes a contract file that declares
another lane, realm and role: the same handler then runs the whole flow, so the refusal is the
guard reading the contract, not an accident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_keycloak_client_effect.handlers import (
    HandlerDevKeycloakClient,
    ModelDevKeycloakClientCommand,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.overlay import (
    ModelDevKeycloakClientOverlay,
)

from .fakes import (
    FakeCredentialSource,
    FakeKeycloak,
    FakeKeyGenerator,
    FakeSettingStore,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect/contract.yaml"
)

_ADMIN_USERNAME = "bootstrap-admin"
_ADMIN_PASSWORD = "bootstrap-password-value"
_SECRET = "Gk7!dev-client-secret"


def _overlay(
    lane: str = "dev",
    realm: str = "omninode",
    roles: tuple[str, ...] = ("manage-users", "view-users"),
    sign_in_client_id: str = "omnidash-spa",
) -> ModelDevKeycloakClientOverlay:
    return ModelDevKeycloakClientOverlay(
        lane=lane,
        keycloak={
            "base_url": "https://keycloak.example.invalid",
            "realm": realm,
            "admin_realm": "master",
            "timeout_seconds": 10.0,
            "token_path_template": "/realms/{realm}/protocol/openid-connect/token",
            "clients_path_template": "/admin/realms/{realm}/clients",
            "client_path_template": "/admin/realms/{realm}/clients/{client_uuid}",
            "service_account_user_path_template": "/admin/realms/{realm}/clients/{client_uuid}/service-account-user",
            "client_role_path_template": "/admin/realms/{realm}/clients/{client_uuid}/roles/{role}",
            "user_client_role_mappings_path_template": "/admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_uuid}",
            "client_scope_mappings_path_template": "/admin/realms/{realm}/clients/{client_uuid}/scope-mappings/clients/{management_client_uuid}",
            "client_protocol_mappers_path_template": "/admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models",
            "client_protocol_mapper_path_template": "/admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models/{mapper_id}",
        },
        bootstrap={
            "env_file": ".example/.env",
            "username_name": "ADMIN_USERNAME",
            "password_name": "ADMIN_PASSWORD",
            "admin_client_id": "admin-cli",
        },
        client={
            "client_id": "provisioner-client",
            "management_client_id": "realm-management",
            "roles": list(roles),
            "secret_key_id": "dev_keycloak_client_secret",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_DEV_CLIENT_SECRET",
        },
        sign_in_client={
            "client_id": sign_in_client_id,
            "mappers": [
                {
                    "name": "tenant_id",
                    "protocol_mapper": "oidc-usermodel-attribute-mapper",
                    "config": {
                        "user.attribute": "tenant_id",
                        "claim.name": "tenant_id",
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "userinfo.token.claim": "true",
                        "jsonType.label": "String",
                    },
                },
                {
                    "name": "tenant_slug",
                    "protocol_mapper": "oidc-usermodel-attribute-mapper",
                    "config": {
                        "user.attribute": "tenant_slug",
                        "claim.name": "tenant_slug",
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "userinfo.token.claim": "true",
                        "jsonType.label": "String",
                    },
                },
            ],
        },
        secret_store=None,
    )


@dataclass
class _Rig:
    handler: HandlerDevKeycloakClient
    transport: FakeKeycloak
    store: FakeSettingStore
    gen: FakeKeyGenerator
    credentials: FakeCredentialSource


def _build(
    overlay: ModelDevKeycloakClientOverlay,
    contract_path: Path | None = None,
    known_roles: tuple[str, ...] = ("manage-users", "view-users"),
) -> _Rig:
    transport = FakeKeycloak(known_roles=known_roles, realm=overlay.keycloak.realm)
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_DEV_CLIENT_SECRET", _SECRET)
    gen = FakeKeyGenerator(outcome="created")
    credentials = FakeCredentialSource(
        {"ADMIN_USERNAME": _ADMIN_USERNAME, "ADMIN_PASSWORD": _ADMIN_PASSWORD}
    )
    handler = HandlerDevKeycloakClient(
        overlay=overlay,
        transport=transport,
        settings_store=store,
        key_generator=gen,
        credential_source=credentials,
        contract_path=contract_path or CONTRACT_PATH,
    )
    return _Rig(
        handler=handler,
        transport=transport,
        store=store,
        gen=gen,
        credentials=credentials,
    )


def _assert_zero_calls(rig: _Rig) -> None:
    assert rig.transport.call_log == []
    assert rig.transport.sent == []
    assert rig.store.calls == []
    assert rig.gen.calls == []
    assert rig.credentials.calls == []


def test_non_dev_lane_in_command_is_refused() -> None:
    rig = _build(_overlay())
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="prod", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "lane" in result.detail
    assert "prod" in result.detail
    _assert_zero_calls(rig)


def test_non_dev_lane_in_overlay_is_refused() -> None:
    rig = _build(_overlay(lane="prod"))
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "lane" in result.detail
    assert "prod" in result.detail
    _assert_zero_calls(rig)


def test_undeclared_realm_is_refused() -> None:
    rig = _build(_overlay(realm="other-realm"))
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "realm" in result.detail
    assert "other-realm" in result.detail
    _assert_zero_calls(rig)


def test_a_role_outside_the_allowed_roles_is_refused() -> None:
    rig = _build(_overlay(roles=("manage-clients",)))
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_role_not_allowed"
    assert "manage-clients" in result.detail
    _assert_zero_calls(rig)


def test_a_sign_in_client_outside_the_mapper_clients_is_refused() -> None:
    rig = _build(_overlay(sign_in_client_id="rogue-spa"))
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_mapper_client_not_allowed"
    assert "rogue-spa" in result.detail
    _assert_zero_calls(rig)


def test_a_probe_with_an_undeclared_lane_is_refused_too() -> None:
    rig = _build(_overlay())
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="staging", write=False))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    _assert_zero_calls(rig)


def test_failing_control_a_contract_declaring_the_other_lane_runs_the_flow(
    tmp_path: Path,
) -> None:
    """A contract file declaring another lane, realm and role lets the same handler run."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["dev_only"] = {
        "lane": "other",
        "realms": ["other-realm"],
        "allowed_roles": ["manage-users", "view-users", "manage-clients"],
        "mapper_clients": ["omnidash-spa"],
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")

    rig = _build(
        _overlay(
            lane="other",
            realm="other-realm",
            roles=("manage-users", "view-users", "manage-clients"),
        ),
        contract_path=contract_path,
        known_roles=("manage-users", "view-users", "manage-clients"),
    )
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="other", write=True))
    )
    assert result.ok is True
    assert result.outcome.value == "ensured"
    assert result.token_status == 200
    # Three roles granted: fifteen calls, the one extra being the third role lookup, plus the
    # sign-in client lookup, the protocol mapper collection and the two mapper creates.
    assert len(rig.transport.call_log) == 15


def test_the_declared_dev_only_block_is_the_contract_file_s(tmp_path: Path) -> None:
    """The guard's lane, realms and roles come from the contract file, not from code."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["dev_only"] == {
        "lane": "dev",
        "realms": ["omninode"],
        "allowed_roles": ["manage-users", "view-users"],
        "mapper_clients": ["omnidash-spa"],
    }

    # And the guard honors a changed file: a dev command against a staging-only contract is refused.
    contract["dev_only"]["lane"] = "staging"
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")
    rig = _build(_overlay(), contract_path=contract_path)
    result = asyncio.run(
        rig.handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.outcome.value == "refused_not_dev"
    _assert_zero_calls(rig)
