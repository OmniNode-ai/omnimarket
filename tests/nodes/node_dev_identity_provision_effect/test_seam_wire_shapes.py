# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire shapes the handler sends are the shapes the real Keycloak and onex-api speak (OMN-24115).

The strict fake accepts the handler's end-to-end flow: the token form field names, the admin bearer
on the admin paths, the user create body's keys and the user's bearer on the provision path. Each
failing control renames one field of one request at the wire and shows the fake refusing it, so a
changed wire shape on either side fails here rather than against a real realm. The contract's
dev_only block and topics are pinned too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
)
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

BASE = "https://keycloak.example.invalid"
TOKEN_URL = f"{BASE}/realms/omninode/protocol/openid-connect/token"
USERS_URL = f"{BASE}/admin/realms/omninode/users"
PROVISION_URL = "https://api.example.invalid/v1/auth/provision"


def _overlay(**overrides: object) -> ModelDevIdentityProvisionOverlay:
    fields: dict[str, object] = {
        "lane": "dev",
        "keycloak": {
            "base_url": BASE,
            "realm": "omninode",
            "timeout_seconds": 10.0,
            "token_path_template": "/realms/{realm}/protocol/openid-connect/token",
            "admin_users_path_template": "/admin/realms/{realm}/users",
            "admin_reset_password_path_template": "/admin/realms/{realm}/users/{user_id}/reset-password",
        },
        "admin": {
            "client_id": "admin-client",
            "secret_folder": "/shared/env",
            "secret_key": "KEYCLOAK_ADMIN_CLIENT_SECRET",
        },
        "user": {
            "username": "dev-probe-user",
            "email": "dev-probe@example.invalid",
            "first_name": "Dev",
            "last_name": "Probe",
            "client_id": "public-client",
            "password_key_id": "dev_probe_user_password",
            "password_folder": "/shared/env",
            "password_key": "DEV_PROBE_USER_PASSWORD",
        },
        "api": {
            "base_url": "https://api.example.invalid",
            "provision_path": "/v1/auth/provision",
            "timeout_seconds": 15.0,
        },
        "secret_store": None,
    }
    fields.update(overrides)
    return ModelDevIdentityProvisionOverlay.model_validate(fields)


def _rig(
    overlay: ModelDevIdentityProvisionOverlay,
) -> tuple[HandlerDevIdentityProvision, FakeKeycloakAndApi]:
    transport = FakeKeycloakAndApi()
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", "admin-secret-value")
    store.set("/shared/env", "DEV_PROBE_USER_PASSWORD", "Dp1!generated")
    gen = FakeKeyGenerator(outcome="created")
    handler = HandlerDevIdentityProvision(
        overlay=overlay,
        transport=transport,
        settings_store=store,
        key_generator=gen,
        contract_path=CONTRACT_PATH,
    )
    return handler, transport


def test_the_handler_flow_is_accepted_by_the_strict_fake() -> None:
    handler, _ = _rig(_overlay())
    result = asyncio.run(
        handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )
    assert result.ok is True
    assert result.outcome.value == "provisioned"


async def test_failing_control_a_renamed_token_form_field_is_refused() -> None:
    """The admin token form names grant_type, client_id and client_secret; a rename is refused."""
    transport = FakeKeycloakAndApi()
    renamed = await transport.send(
        ModelIdentityHttpRequest(
            method="POST",
            url=TOKEN_URL,
            form={
                "grant_type": SecretStr("client_credentials"),
                "client": SecretStr("admin-client"),
                "client_secret": SecretStr("admin-secret-value"),
            },
            timeout_seconds=10.0,
        )
    )
    assert renamed.status != 200
    assert renamed.json_body is None or "access_token" not in renamed.json_body


async def test_failing_control_a_renamed_user_create_body_key_is_refused() -> None:
    """The create body carries username, enabled and credentials; a renamed username is refused."""
    transport = FakeKeycloakAndApi()
    token = await transport.send(
        ModelIdentityHttpRequest(
            method="POST",
            url=TOKEN_URL,
            form={
                "grant_type": SecretStr("client_credentials"),
                "client_id": SecretStr("admin-client"),
                "client_secret": SecretStr("admin-secret-value"),
            },
            timeout_seconds=10.0,
        )
    )
    assert token.status == 200
    assert token.json_body is not None
    bearer = SecretStr(token.json_body["access_token"])
    renamed = await transport.send(
        ModelIdentityHttpRequest(
            method="POST",
            url=USERS_URL,
            bearer=bearer,
            json_body={
                "user_name": "dev-probe-user",
                "email": "dev-probe@example.invalid",
                "enabled": True,
                "credentials": [
                    {"type": "password", "value": "Dp1!generated", "temporary": False}
                ],
            },
            timeout_seconds=10.0,
        )
    )
    assert renamed.status == 400


async def test_failing_control_a_wrong_bearer_on_provision_is_refused() -> None:
    """The provision endpoint binds the bearer's user; the admin's bearer is refused there."""
    transport = FakeKeycloakAndApi()
    token = await transport.send(
        ModelIdentityHttpRequest(
            method="POST",
            url=TOKEN_URL,
            form={
                "grant_type": SecretStr("client_credentials"),
                "client_id": SecretStr("admin-client"),
                "client_secret": SecretStr("admin-secret-value"),
            },
            timeout_seconds=10.0,
        )
    )
    assert token.status == 200
    assert token.json_body is not None
    admin_bearer = SecretStr(token.json_body["access_token"])
    refused = await transport.send(
        ModelIdentityHttpRequest(
            method="POST",
            url=PROVISION_URL,
            bearer=admin_bearer,
            timeout_seconds=15.0,
        )
    )
    assert refused.status == 403


def test_the_contract_declares_the_dev_only_block() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["dev_only"] == {"lane": "dev", "realms": ["omninode"]}


def test_the_contract_topics_follow_the_topic_grammar() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    topics = contract["topics"]
    for name in ("command", "completed", "failed"):
        topic = topics[name]
        parts = topic.split(".")
        assert parts[0] == "onex"
        assert parts[1] in ("cmd", "evt")
        assert parts[2] == "omnimarket"
        assert parts[3].startswith("dev-identity-provision-")
        assert parts[4].startswith("v")
        assert parts[4][1:].isdigit()
    assert (
        topics["command"] == "onex.cmd.omnimarket.dev-identity-provision-requested.v1"
    )
    assert (
        topics["completed"] == "onex.evt.omnimarket.dev-identity-provision-completed.v1"
    )
    assert topics["failed"] == "onex.evt.omnimarket.dev-identity-provision-failed.v1"
