# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for the dev identity provision node (OMN-24115).

Each case injects one named break -- a missing credential, a refused token, an unreachable host, a
generator that refuses the declared id, a store that holds no password, a refused user create, a
refused sign-in, a refused provision, a malformed provision body -- and shows the one typed outcome
for it, ``ok`` false, and no secret in the result's dump, repr or str.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)

from .fakes import FakeKeycloakAndApi, FakeKeyGenerator, FakeSettingStore

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_identity_provision_effect/contract.yaml"
)

_ADMIN_SECRET = "admin-secret-value"
_PASSWORD = "Dp1!generated"
_ADMIN_TOKEN = "admin-access-token"
_USER_TOKEN = "user-access-token"
_BOOTSTRAP_TOKEN = "bootstrap-token-xyz"


def _overlay() -> ModelDevIdentityProvisionOverlay:
    return ModelDevIdentityProvisionOverlay(
        lane="dev",
        keycloak={
            "base_url": "https://keycloak.example.invalid",
            "realm": "omninode",
            "timeout_seconds": 10.0,
            "token_path_template": "/realms/{realm}/protocol/openid-connect/token",
            "admin_users_path_template": "/admin/realms/{realm}/users",
            "admin_reset_password_path_template": "/admin/realms/{realm}/users/{user_id}/reset-password",
        },
        admin={
            "client_id": "admin-client",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_ADMIN_CLIENT_SECRET",
        },
        user={
            "username": "dev-probe-user",
            "email": "dev-probe@example.invalid",
            "first_name": "Dev",
            "last_name": "Probe",
            "client_id": "public-client",
            "password_key_id": "dev_probe_user_password",
            "password_folder": "/shared/env",
            "password_key": "DEV_PROBE_USER_PASSWORD",
        },
        api={
            "base_url": "https://api.example.invalid",
            "provision_path": "/v1/auth/provision",
            "timeout_seconds": 15.0,
        },
        secret_store=None,
    )


def _build(
    transport: FakeKeycloakAndApi,
    store: FakeSettingStore,
    gen: FakeKeyGenerator,
) -> HandlerDevIdentityProvision:
    return HandlerDevIdentityProvision(
        overlay=_overlay(),
        transport=transport,
        settings_store=store,
        key_generator=gen,
        contract_path=CONTRACT_PATH,
    )


def _seed(store: FakeSettingStore, *, with_password: bool = True) -> None:
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", _ADMIN_SECRET)
    if with_password:
        store.set("/shared/env", "DEV_PROBE_USER_PASSWORD", _PASSWORD)


def _assert_no_secret(result: object) -> None:
    """No token, secret or password in the result's dump, repr or str."""
    dump = result.model_dump_json()
    for text in (dump, repr(result), str(result)):
        for secret in (
            _PASSWORD,
            _ADMIN_SECRET,
            _ADMIN_TOKEN,
            _USER_TOKEN,
            _BOOTSTRAP_TOKEN,
        ):
            assert secret not in text


def test_admin_credential_missing_names_the_folder_and_key_name() -> None:
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()  # no credential seeded
    gen = FakeKeyGenerator()
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "admin_credential_missing"
    # The detail and expected_secret name the folder and the key NAME the node expected.
    assert result.expected_secret == "/shared/env/KEYCLOAK_ADMIN_CLIENT_SECRET"
    assert "/shared/env" in result.detail
    assert "KEYCLOAK_ADMIN_CLIENT_SECRET" in result.detail
    # Zero HTTP calls: the refusal came before any call.
    assert transport.call_log == []
    assert transport.sent == []
    assert gen.calls == []
    _assert_no_secret(result)


def test_admin_token_refused() -> None:
    transport = FakeKeycloakAndApi(fail_admin_token=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator()
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "admin_token_refused"
    assert gen.calls == []
    _assert_no_secret(result)


def test_keycloak_unreachable() -> None:
    transport = FakeKeycloakAndApi(raise_connection_error=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator()
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "keycloak_unreachable"
    assert "ConnectionError" in result.detail
    assert gen.calls == []
    _assert_no_secret(result)


def test_key_generator_invalid_request_is_password_unavailable() -> None:
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="invalid_request")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "password_unavailable"
    assert "dev_probe_user_password" in result.detail
    # The admin token was taken but no user was looked up and nothing was created.
    assert len(transport.call_log) == 1
    _assert_no_secret(result)


def test_password_read_none_is_password_unavailable() -> None:
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    _seed(store, with_password=False)  # the generator ran, the store holds no password
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "password_unavailable"
    assert "/shared/env" in result.detail
    assert "DEV_PROBE_USER_PASSWORD" in result.detail
    _assert_no_secret(result)


def test_user_create_failed() -> None:
    transport = FakeKeycloakAndApi(fail_user_create=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "user_create_failed"
    assert result.user_name == "dev-probe-user"
    _assert_no_secret(result)


def test_user_token_refused() -> None:
    transport = FakeKeycloakAndApi(fail_user_token=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "user_token_refused"
    assert result.user_name == "dev-probe-user"
    _assert_no_secret(result)


def test_provision_refused() -> None:
    transport = FakeKeycloakAndApi(fail_provision=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "provision_refused"
    _assert_no_secret(result)


def test_provision_body_missing_tenant_id_is_response_invalid() -> None:
    transport = FakeKeycloakAndApi(provision_missing_tenant_id=True)
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "provision_response_invalid"
    assert result.tenant_id is None
    assert result.tenant_slug is None
    _assert_no_secret(result)


def test_api_unreachable() -> None:
    transport = FakeKeycloakAndApi(
        raise_connection_error=True, raise_connection_error_on="/provision"
    )
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created")
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is False
    assert result.outcome.value == "api_unreachable"
    assert "ConnectionError" in result.detail
    _assert_no_secret(result)
