# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the dev keycloak client node (OMN-24237).

One run against the fakes ensures the dev realm's service-account client: the call order is the
admin token, the clients lookup, the client create, the management client lookup, the service
account user, the role lookups, the role mapping, the scope mapping, the sign-in client lookup,
the protocol mapper collection and one mapper create per declared mapper, then the client token.
The generated secret moves to Keycloak -- the client representation and the client_credentials
form -- and to nothing else: not the result's JSON dump, its repr, its str or the logs, and
neither does the admin password or any access token. A second run finds the client and PUTs the
same secret and the existing mappers; the probe verb answers ready and calls nothing.
"""

from __future__ import annotations

import asyncio
import logging
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

_BASE = "https://keycloak.example.invalid"
_REALM = "omninode"
_ADMIN_REALM = "master"
_CLIENTS_SUFFIX = "/admin/realms/omninode/clients"
_MAPPERS_FRAGMENT = "/protocol-mappers/models"
_ADMIN_USERNAME = "bootstrap-admin"
_ADMIN_PASSWORD = "bootstrap-password-value"
_SECRET = "Gk7!dev-client-secret"
_ADMIN_TOKEN = "admin-access-token"
_CLIENT_TOKEN = "client-access-token"


def _overlay() -> ModelDevKeycloakClientOverlay:
    return ModelDevKeycloakClientOverlay(
        lane="dev",
        keycloak={
            "base_url": _BASE,
            "realm": _REALM,
            "admin_realm": _ADMIN_REALM,
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
            "roles": ["manage-users", "view-users"],
            "secret_key_id": "dev_keycloak_client_secret",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_DEV_CLIENT_SECRET",
        },
        sign_in_client={
            "client_id": "omnidash-spa",
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


def _assert_secretless(result: object, caplog_text: str) -> None:
    """No secret, password or token in the result's dump, repr, str or the logs."""
    dump = result.model_dump_json()
    for text in (dump, repr(result), str(result), caplog_text):
        for secret in (
            _SECRET,
            _ADMIN_PASSWORD,
            _ADMIN_TOKEN,
            _CLIENT_TOKEN,
        ):
            assert secret not in text


def test_golden_chain_ensured(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloak()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert result.ok is True
    assert result.outcome.value == "ensured"
    assert result.token_status == 200
    assert result.client_id == "provisioner-client"
    assert result.realm == "omninode"
    assert result.lane == "dev"
    assert result.mappers_ensured == 2

    # Call order: admin token, clients lookup, client create, management client lookup, service
    # account user, role lookups, role mapping, scope mapping, sign-in client lookup, protocol
    # mapper collection, one mapper create per declared mapper, client token.
    calls = transport.call_log
    assert len(calls) == 14
    expected = [
        ("POST", f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token"),
        ("GET", _CLIENTS_SUFFIX),
        ("POST", _CLIENTS_SUFFIX),
        ("GET", _CLIENTS_SUFFIX),
        ("GET", "/service-account-user"),
        ("GET", "/roles/manage-users"),
        ("GET", "/roles/view-users"),
        ("POST", "/role-mappings/clients/"),
        ("POST", "/scope-mappings/clients/"),
        ("GET", _CLIENTS_SUFFIX),
        ("GET", _MAPPERS_FRAGMENT),
        ("POST", _MAPPERS_FRAGMENT),
        ("POST", _MAPPERS_FRAGMENT),
        ("POST", f"/realms/{_REALM}/protocol/openid-connect/token"),
    ]
    for call, (method, fragment) in zip(calls, expected, strict=True):
        assert call.startswith(method)
        assert fragment in call

    # The empty dev-client lookup came first, the management client lookup second and the
    # sign-in client lookup after the role grants.
    listings = [
        request.query["clientId"]
        for request in transport.sent
        if request.method == "GET" and request.url.endswith(_CLIENTS_SUFFIX)
    ]
    assert listings == ["provisioner-client", "realm-management", "omnidash-spa"]

    # Every admin call carried the admin bearer; neither token call carried one.
    admin_calls = [
        request for request in transport.sent if "/admin/realms/" in request.url
    ]
    assert len(admin_calls) == 12
    assert all(
        request.bearer is not None and request.bearer.get_secret_value() == _ADMIN_TOKEN
        for request in admin_calls
    )
    token_calls = [
        request for request in transport.sent if "openid-connect/token" in request.url
    ]
    assert len(token_calls) == 2
    assert all(request.bearer is None for request in token_calls)

    # The generated secret reached the Keycloak client representation.
    assert len(transport.created_bodies) == 1
    assert transport.created_bodies[0]["clientId"] == "provisioner-client"
    assert transport.created_bodies[0]["secret"] == _SECRET
    assert transport.received_secret == _SECRET

    # ... and the client_credentials form, which is the whole of what the token forms carried.
    assert transport.token_forms == [
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _ADMIN_USERNAME,
            "password": _ADMIN_PASSWORD,
        },
        {
            "grant_type": "client_credentials",
            "client_id": "provisioner-client",
            "client_secret": _SECRET,
        },
    ]

    # The role grants carried the roles' ids and names, never the secret.
    for body in transport.role_mapping_bodies + transport.scope_mapping_bodies:
        assert [set(entry) for entry in body] == [{"id", "name"}] * len(body)
        assert _SECRET not in str(body)

    # No secret, password or token anywhere in the result or the logs.
    _assert_secretless(result, caplog.text)


def test_second_run_finds_the_client_and_puts_the_same_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloak()
    store = FakeSettingStore()
    _seed(store)
    gen = FakeKeyGenerator(outcome="exists", value=_SECRET)
    handler = _build(transport, store, gen, _credentials())

    first = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    second = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )

    assert first.ok is True
    assert second.ok is True
    assert first.outcome.value == "ensured"
    assert second.outcome.value == "ensured"
    assert second.token_status == 200

    # The client was created once; the second run found it and PUT its representation.
    assert len(transport.created_bodies) == 1
    assert len(transport.updated_bodies) == 1
    assert transport.updated_bodies[0]["secret"] == _SECRET
    assert transport.dev_uuid is not None
    puts = [call for call in transport.call_log if call.startswith("PUT ")]
    assert puts[0] == f"PUT {_BASE}/admin/realms/{_REALM}/clients/{transport.dev_uuid}"
    # The second run also found both sign-in client mappers and PUT them in place, creating no more.
    assert len(puts) == 3
    assert all(_MAPPERS_FRAGMENT + "/" in call for call in puts[1:])
    assert len(transport.mapper_created_bodies) == 2
    assert len(transport.mapper_updated_bodies) == 2

    # The roles were granted on both runs, and the client_credentials proof succeeded on both.
    assert len(transport.role_mapping_bodies) == 2
    assert len(transport.scope_mapping_bodies) == 2
    assert transport.received_secret == _SECRET
    _assert_secretless(second, caplog.text)


def test_probe_verb_returns_ready_and_calls_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloak()
    store = FakeSettingStore()
    gen = FakeKeyGenerator()
    credentials = _credentials()
    handler = _build(transport, store, gen, credentials)

    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=False))
    )

    assert result.ok is True
    assert result.outcome.value == "ready"
    assert result.client_id is None
    assert result.token_status is None
    assert result.realm == "omninode"
    assert result.lane == "dev"
    assert transport.call_log == []
    assert transport.sent == []
    # The probe read one credential -- the bootstrap admin -- and generated and stored nothing.
    assert credentials.calls == [["ADMIN_USERNAME", "ADMIN_PASSWORD"]]
    assert gen.calls == []
    assert store.calls == []
    _assert_secretless(result, caplog.text)
