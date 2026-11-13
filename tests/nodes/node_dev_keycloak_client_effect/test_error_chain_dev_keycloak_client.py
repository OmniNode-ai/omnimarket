# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for the dev keycloak client node (OMN-24237).

Each case injects one named break -- a missing bootstrap credential, a refused
admin token, an unreachable host, a generator that refuses the declared id, a
store that holds no client secret, a refused client write, a refused role
grant and a refused client_credentials proof -- and shows the one typed
outcome for it, ``ok`` false, and no secret in the result's dump, repr or str.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
_ADMIN_TOKEN = "admin-access-token"
_CLIENT_TOKEN = "client-access-token"


def _overlay() -> ModelDevKeycloakClientOverlay:
    return ModelDevKeycloakClientOverlay(
        lane="dev",
        keycloak={
            "base_url": "https://keycloak.example.invalid",
            "realm": "omninode",
            "admin_realm": "master",
            "timeout_seconds": 10.0,
            "token_path_template": "/realms/{realm}/protocol/openid-connect/token",
            "clients_path_template": "/admin/realms/{realm}/clients",
            "client_path_template": "/admin/realms/{realm}/clients/{client_uuid}",
            "service_account_user_path_template": "/admin/realms/{realm}/clients/{client_uuid}/service-account-user",
            "client_role_path_template": "/admin/realms/{realm}/clients/{client_uuid}/roles/{role}",
            "user_client_role_mappings_path_template": "/admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_uuid}",
            "client_scope_mappings_path_template": "/admin/realms/{realm}/clients/{client_uuid}/scope-mappings/clients/{management_client_uuid}",
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
            "roles": ["manage-users", "view-users"],
            "secret_key_id": "dev_keycloak_client_secret",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_DEV_CLIENT_SECRET",
        },
        secret_store=None,
    )


def _build(
    transport: FakeKeycloak,
    store: FakeSettingStore,
    gen: FakeKeyGenerator,
    credentials: FakeCredentialSource,
) -> HandlerDevKeycloakClient:
    return HandlerDevKeycloakClient(
        overlay=_overlay(),
        transport=transport,
        settings_store=store,
        key_generator=gen,
        credential_source=credentials,
        contract_path=CONTRACT_PATH,
    )


def _seed(store: FakeSettingStore) -> None:
    store.set("/shared/env", "KEYCLOAK_DEV_CLIENT_SECRET", _SECRET)


def _credentials() -> FakeCredentialSource:
    return FakeCredentialSource(
        {"ADMIN_USERNAME": _ADMIN_USERNAME, "ADMIN_PASSWORD": _ADMIN_PASSWORD}
    )


def _assert_no_secret(result: object) -> None:
    """No token, secret or password in the result's dump, repr or str."""
    dump = result.model_dump_json()
    for text in (dump, repr(result), str(result)):
        for secret in (
            _SECRET,
            _ADMIN_PASSWORD,
            _ADMIN_TOKEN,
            _CLIENT_TOKEN,
        ):
            assert secret not in text


def test_bootstrap_credential_missing_names_the_env_file_and_both_names() -> None:
    transport = FakeKeycloak()
    store = FakeSettingStore()
    gen = FakeKeyGenerator()
    credentials = FakeCredentialSource()  # the env file holds neither name
    handler = _build(transport, store, gen, credentials)

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "bootstrap_credential_missing"
    # The detail and expected_secret name the env file and the variable NAMES the node wanted.
    assert result.expected_secret == ".example/.env:ADMIN_USERNAME,ADMIN_PASSWORD"
    assert ".example/.env" in result.detail
    assert "ADMIN_USERNAME" in result.detail
    assert "ADMIN_PASSWORD" in result.detail
    # Zero HTTP calls: the refusal came before any call.
    assert transport.call_log == []
    assert transport.sent == []
    assert gen.calls == []
    assert store.calls == []
    _assert_no_secret(result)


def test_admin_token_refused() -> None:
    transport = FakeKeycloak(fail_admin_token=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "admin_token_refused"
    # Nothing was generated or stored past a refused admin token.
    assert gen.calls == []
    assert store.calls == []
    # Only the token call happened.
    assert len(transport.call_log) == 1
    _assert_no_secret(result)


def test_keycloak_unreachable() -> None:
    transport = FakeKeycloak(raise_connection_error=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "keycloak_unreachable"
    assert "ConnectionError" in result.detail
    assert gen.calls == []
    _assert_no_secret(result)


def test_client_secret_generator_failure_is_client_secret_unavailable() -> None:
    transport = FakeKeycloak()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="failed")
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "client_secret_unavailable"
    assert "dev_keycloak_client_secret" in result.detail
    # Only the admin token call happened; the store was never asked.
    assert len(transport.call_log) == 1
    assert store.calls == []
    _assert_no_secret(result)


def test_client_secret_read_none_is_client_secret_unavailable() -> None:
    transport = FakeKeycloak()
    store = FakeSettingStore()  # the generator ran, the store holds no secret
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "client_secret_unavailable"
    assert result.expected_secret == "/shared/env/KEYCLOAK_DEV_CLIENT_SECRET"
    assert "/shared/env" in result.detail
    assert "KEYCLOAK_DEV_CLIENT_SECRET" in result.detail
    # The generator ran; only the admin token call reached Keycloak.
    assert gen.calls == ["dev_keycloak_client_secret"]
    assert len(transport.call_log) == 1
    _assert_no_secret(result)


def test_client_write_failed() -> None:
    transport = FakeKeycloak(fail_client_create=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "client_write_failed"
    assert result.client_id == "provisioner-client"
    _assert_no_secret(result)


def test_role_grant_failed() -> None:
    transport = FakeKeycloak(fail_role_mapping=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "role_grant_failed"
    assert result.client_id == "provisioner-client"
    _assert_no_secret(result)


def test_client_token_refused() -> None:
    transport = FakeKeycloak(fail_client_token=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "client_token_refused"
    assert result.token_status == 401
    assert result.client_id == "provisioner-client"
    _assert_no_secret(result)
