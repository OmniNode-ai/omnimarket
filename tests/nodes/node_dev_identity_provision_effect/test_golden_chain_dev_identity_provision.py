# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the dev identity provision node (OMN-24115).

One run against the fakes provisions the probe tenant: the call order is the admin token, the user
lookup, the user create, the user token and the provision call. The generated password moves to
Keycloak (the create body and the password grant) and to nothing else -- not the result's JSON dump,
its repr, its str or the logs -- and neither does any token. A second run finds the user, resets its
stored password and binds the same tenant; the probe verb answers ready and calls nothing.
"""

from __future__ import annotations

import asyncio
import logging
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


def _seed(store: FakeSettingStore) -> None:
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", _ADMIN_SECRET)
    store.set("/shared/env", "DEV_PROBE_USER_PASSWORD", _PASSWORD)


def _assert_secretless(result: object, caplog_text: str) -> None:
    """No token, secret or password in the result's dump, repr, str or the logs."""
    dump = result.model_dump_json()
    for text in (dump, repr(result), str(result), caplog_text):
        for secret in (
            _PASSWORD,
            _ADMIN_SECRET,
            _ADMIN_TOKEN,
            _USER_TOKEN,
            _BOOTSTRAP_TOKEN,
        ):
            assert secret not in text


def test_golden_chain_provisioned(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_PASSWORD)
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert result.ok is True
    assert result.outcome.value == "provisioned"
    assert result.tenant_id == "tenant-abc-123"
    assert result.tenant_slug == "probe-tenant"
    assert result.user_name == "dev-probe-user"
    assert result.realm == "omninode"
    assert result.lane == "dev"

    # Call order: admin token, user lookup, user create, user token, provision.
    calls = transport.call_log
    assert len(calls) == 5
    expected = [
        ("POST", "openid-connect/token"),
        ("GET", "/users"),
        ("POST", "/users"),
        ("POST", "openid-connect/token"),
        ("POST", "/provision"),
    ]
    for call, (method, fragment) in zip(calls, expected, strict=True):
        assert call.startswith(method)
        assert fragment in call

    # The generated password was passed to Keycloak: the create body's credential.
    assert len(transport.create_bodies) == 1
    assert transport.create_bodies[0]["username"] == "dev-probe-user"
    assert transport.create_bodies[0]["credentials"][0]["value"] == _PASSWORD
    # and the password grant's form.
    grants = [f for f in transport.grant_forms if f.get("grant_type") == "password"]
    assert grants
    assert grants[0]["client_id"] == "public-client"
    assert grants[0]["username"] == "dev-probe-user"
    assert grants[0]["password"] == _PASSWORD

    # No token, secret or password anywhere in the result or the logs.
    _assert_secretless(result, caplog.text)


def test_second_run_reuses_the_user_and_resets_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="exists", value=_PASSWORD)
    handler = _build(transport, store, gen)

    first = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )
    second = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )

    assert first.ok is True
    assert second.ok is True
    assert first.tenant_id == "tenant-abc-123"
    assert second.tenant_id == first.tenant_id
    assert second.tenant_slug == first.tenant_slug
    # The user was created once; the second run found it and reset its stored password.
    assert len(transport.create_bodies) == 1
    assert len(transport.reset_bodies) == 1
    assert transport.reset_bodies[0] == {
        "type": "password",
        "value": _PASSWORD,
        "temporary": False,
    }
    assert any("reset-password" in c for c in transport.call_log)
    _assert_secretless(second, caplog.text)


def test_probe_verb_returns_ready_and_calls_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", _ADMIN_SECRET)
    gen = FakeKeyGenerator()
    handler = _build(transport, store, gen)

    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=False))
    )

    assert result.ok is True
    assert result.outcome.value == "ready"
    assert result.user_name is None
    assert result.tenant_id is None
    assert transport.call_log == []
    assert transport.sent == []
    # The probe read one name -- the admin credential -- and generated nothing.
    assert store.calls == [("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET")]
    assert gen.calls == []
    _assert_secretless(result, caplog.text)
