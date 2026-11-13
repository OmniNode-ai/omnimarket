# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire shapes the handler sends are the shapes the real Keycloak speaks (OMN-24237).

The strict fake accepts the handler's end-to-end flow: the token form field
names, the admin bearer on the admin paths and the client representation's
confidential service-account keys. Each failing control renames one field of
one request at the wire -- a token form field sent raw, or the representation
key a fake configured to expect last year's shape requires -- and shows the
run failing here rather than against a real realm. The contract's topics are
pinned too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_dev_keycloak_client_effect.handlers import (
    HandlerDevKeycloakClient,
    ModelDevKeycloakClientCommand,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_keycloak_client_http import (
    ModelKeycloakHttpRequest,
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

BASE = "https://keycloak.example.invalid"
ADMIN_TOKEN_URL = f"{BASE}/realms/master/protocol/openid-connect/token"
CLIENT_TOKEN_URL = f"{BASE}/realms/omninode/protocol/openid-connect/token"

_ADMIN_USERNAME = "bootstrap-admin"
_ADMIN_PASSWORD = "bootstrap-password-value"
_SECRET = "Gk7!dev-client-secret"


def _overlay(**overrides: object) -> ModelDevKeycloakClientOverlay:
    fields: dict[str, object] = {
        "lane": "dev",
        "keycloak": {
            "base_url": BASE,
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
        "bootstrap": {
            "env_file": ".example/.env",
            "username_name": "ADMIN_USERNAME",
            "password_name": "ADMIN_PASSWORD",
            "admin_client_id": "admin-cli",
        },
        "client": {
            "client_id": "provisioner-client",
            "management_client_id": "realm-management",
            "roles": ["manage-users", "view-users"],
            "secret_key_id": "dev_keycloak_client_secret",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_DEV_CLIENT_SECRET",
        },
        "secret_store": None,
    }
    fields.update(overrides)
    return ModelDevKeycloakClientOverlay.model_validate(fields)


def _rig(
    overlay: ModelDevKeycloakClientOverlay,
    transport: FakeKeycloak | None = None,
) -> tuple[HandlerDevKeycloakClient, FakeKeycloak]:
    transport = transport or FakeKeycloak()
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_DEV_CLIENT_SECRET", _SECRET)
    gen = FakeKeyGenerator(outcome="created", value=_SECRET)
    credentials = FakeCredentialSource(
        {"ADMIN_USERNAME": _ADMIN_USERNAME, "ADMIN_PASSWORD": _ADMIN_PASSWORD}
    )
    handler = HandlerDevKeycloakClient(
        overlay=overlay,
        transport=transport,
        settings_store=store,
        key_generator=gen,
        credential_source=credentials,
        contract_path=CONTRACT_PATH,
    )
    return handler, transport


def test_the_handler_flow_is_accepted_by_the_strict_fake() -> None:
    handler, _ = _rig(_overlay())
    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is True
    assert result.outcome.value == "ensured"


async def test_failing_control_a_renamed_token_form_field_is_refused() -> None:
    """The client_credentials form names grant_type, client_id and client_secret; a rename is refused."""
    transport = FakeKeycloak()
    renamed = await transport.send(
        ModelKeycloakHttpRequest(
            method="POST",
            url=CLIENT_TOKEN_URL,
            form={
                "grant_type": SecretStr("client_credentials"),
                "client": SecretStr("provisioner-client"),
                "client_secret": SecretStr(_SECRET),
            },
            timeout_seconds=10.0,
        )
    )
    assert renamed.status != 200
    assert renamed.json_body is None or "access_token" not in renamed.json_body


class _WantsRenamedServiceAccountFlag(FakeKeycloak):
    """A Keycloak expecting last year's representation: serviceAccountEnabled, no s."""

    def _body_problem(self, body: Any) -> str | None:
        if not isinstance(body, dict):
            return "the body is not an object"
        if body.get("serviceAccountEnabled") is not True:
            return "the renamed serviceAccountEnabled is missing"
        return None


def test_failing_control_a_renamed_representation_key_fails_the_client_write() -> None:
    """The representation names serviceAccountsEnabled; a Keycloak wanting serviceAccountEnabled refuses it."""
    handler, _ = _rig(_overlay(), transport=_WantsRenamedServiceAccountFlag())
    result = asyncio.run(
        handler.handle(ModelDevKeycloakClientCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "client_write_failed"
    assert result.client_id == "provisioner-client"


def test_the_contract_topics_follow_the_topic_grammar() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    topics = contract["topics"]
    for name in ("command", "completed", "failed"):
        topic = topics[name]
        parts = topic.split(".")
        assert parts[0] == "onex"
        assert parts[1] in ("cmd", "evt")
        assert parts[2] == "omnimarket"
        assert parts[3].startswith("dev-keycloak-client-")
        assert parts[4].startswith("v")
        assert parts[4][1:].isdigit()
    assert topics["command"] == "onex.cmd.omnimarket.dev-keycloak-client-requested.v1"
    assert topics["completed"] == "onex.evt.omnimarket.dev-keycloak-client-completed.v1"
    assert topics["failed"] == "onex.evt.omnimarket.dev-keycloak-client-failed.v1"
