# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""User-only mode of the dev identity node (OMN-25103).

One Keycloak user in the dev realm with a named realm role, a generated password of its own key, and no tenant: the run never
asks the runtime for a probe tenant and never reads or binds an existing one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dev_identity_provision_effect.handlers import (
    HandlerDevIdentityProvision,
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityCleanup,
    EnumDevIdentityOutcome,
)

from .fakes import (
    FakeKeycloakAndApi,
    FakeKeyGenerator,
    FakeSettingStore,
    FakeTenantProvisioner,
)
from .test_golden_chain_dev_identity_provision import (
    _ADMIN_SECRET,
    CONTRACT_PATH,
    _overlay,
)

pytestmark = pytest.mark.unit

STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
KEY_ID = "dev_dashboard_reader_password"
USER = "dev-dashboard-reader"
PASSWORD = "Dp1!readerpw"


def _command(**over: object) -> ModelDevIdentityProvisionCommand:
    fields: dict[str, object] = {
        "lane": "dev",
        "write": True,
        "username": USER,
        "email": "dev-dashboard-reader@example.invalid",
        "password_key_id": KEY_ID,
        "user_only": True,
        "read_role": "dashboard-reader",
    }
    fields.update(over)
    return ModelDevIdentityProvisionCommand(**fields)  # type: ignore[arg-type]


def _handler(
    transport: FakeKeycloakAndApi, provisioner: FakeTenantProvisioner
) -> HandlerDevIdentityProvision:
    store = FakeSettingStore()
    store.set("/shared/env", "KEYCLOAK_ADMIN_CLIENT_SECRET", _ADMIN_SECRET)
    store.set("/shared/env", KEY_ID.upper(), PASSWORD)
    return HandlerDevIdentityProvision(
        overlay=_overlay(),
        transport=transport,
        settings_store=store,
        key_generator=FakeKeyGenerator(outcome="created", value=PASSWORD),
        tenant_provisioner=provisioner,
        contract_path=CONTRACT_PATH,
    )


async def test_user_only_creates_one_user_with_role_and_no_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeKeycloakAndApi(username=USER, password=PASSWORD)
    provisioner = FakeTenantProvisioner()
    result = await _handler(transport, provisioner).handle(_command())

    assert result.outcome is EnumDevIdentityOutcome.USER_PROVISIONED
    assert result.ok is True
    assert result.tenant_id is None
    assert result.cleanup is EnumDevIdentityCleanup.KEPT
    assert len(transport.create_bodies) == 1
    assert transport.create_bodies[0]["username"] == USER
    assert transport.role_mappings == [
        ("user-1", [{"id": "role-id-reader", "name": "dashboard-reader"}])
    ]
    for text in (result.model_dump_json(), caplog.text):
        assert PASSWORD not in text
        assert _ADMIN_SECRET not in text


async def test_user_only_unknown_role_is_refused_and_user_removed() -> None:
    transport = FakeKeycloakAndApi(username=USER, password=PASSWORD)
    result = await _handler(transport, FakeTenantProvisioner()).handle(
        _command(read_role="no-such-role")
    )
    assert result.outcome is EnumDevIdentityOutcome.ROLE_NOT_FOUND
    assert result.ok is False
    assert result.cleanup is EnumDevIdentityCleanup.DELETED
    assert transport.role_mappings == []


async def test_user_only_refuses_a_non_dev_lane_before_any_call() -> None:
    transport = FakeKeycloakAndApi(username=USER, password=PASSWORD)
    result = await _handler(transport, FakeTenantProvisioner()).handle(
        _command(lane="stability")
    )
    assert result.outcome is EnumDevIdentityOutcome.REFUSED_NOT_DEV
    assert transport.call_log == []


@pytest.mark.parametrize(
    "over",
    [
        {"read_role": None},
        {"username": None},
        {"password_key_id": None},
        {"email": None},
        {"write": False},
        {"tenant_id": "6f1d1c52-5a43-4a0e-9a31-7f9d0c4c2b11"},
        {"lookup_user": True, "write": False},
    ],
)
def test_user_only_command_is_validated(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _command(**over)


def test_read_role_without_user_only_is_refused() -> None:
    with pytest.raises(ValidationError):
        _command(user_only=False)


def test_contract_declares_user_only_paths_and_the_store_declares_the_key() -> None:
    node = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert node["user_only"]["realm_roles_path_template"].endswith("/roles")
    assert "{user_id}" in node["user_only"]["role_mapping_path_template"]
    key = yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"][
        KEY_ID
    ]
    assert key["folder"] == "/shared/env"
    assert key["key"] == "DEV_DASHBOARD_READER_PASSWORD"
    assert key["key"] != "DEV_PROBE_USER_PASSWORD"


def test_store_declares_a_password_key_for_the_pool_proof_tenant_user() -> None:
    keys = yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"]
    key = keys["dev_pool_tenant_password"]
    assert key["folder"] == "/shared/env"
    assert key["key"] == "DEV_POOL_TENANT_PASSWORD"
    assert key["key"] not in {
        "DEV_PROBE_USER_PASSWORD",
        "DEV_DASHBOARD_READER_PASSWORD",
    }
