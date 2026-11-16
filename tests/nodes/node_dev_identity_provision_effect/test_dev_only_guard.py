# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-only guard refuses any lane or realm the contract does not declare (OMN-24115).

A non-dev lane in the command, a non-dev lane in the overlay and a realm the contract does not
declare are each ``refused_not_dev`` with zero calls to the transport, the store and the generator.
The failing control writes a contract file that declares another lane and realm: the same handler
then runs the whole flow, so the refusal is the guard reading the contract, not an accident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

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


def _overlay(
    lane: str = "dev", realm: str = "omninode"
) -> ModelDevIdentityProvisionOverlay:
    return ModelDevIdentityProvisionOverlay(
        lane=lane,
        keycloak={
            "base_url": "https://keycloak.example.invalid",
            "realm": realm,
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


@dataclass
class _Rig:
    handler: HandlerDevIdentityProvision
    transport: FakeKeycloakAndApi
    store: FakeSettingStore
    gen: FakeKeyGenerator


def _build(
    overlay: ModelDevIdentityProvisionOverlay,
    contract_path: Path | None = None,
) -> _Rig:
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
        contract_path=contract_path or CONTRACT_PATH,
    )
    return _Rig(handler=handler, transport=transport, store=store, gen=gen)


def _assert_zero_calls(rig: _Rig) -> None:
    assert rig.transport.call_log == []
    assert rig.transport.sent == []
    assert rig.store.calls == []
    assert rig.gen.calls == []


def test_non_dev_lane_in_command_is_refused() -> None:
    rig = _build(_overlay())
    result = asyncio.run(
        rig.handler.handle(ModelDevIdentityProvisionCommand(lane="prod", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "lane" in result.detail
    assert "prod" in result.detail
    _assert_zero_calls(rig)


def test_non_dev_lane_in_overlay_is_refused() -> None:
    rig = _build(_overlay(lane="prod"))
    result = asyncio.run(
        rig.handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "lane" in result.detail
    assert "prod" in result.detail
    _assert_zero_calls(rig)


def test_undeclared_realm_is_refused() -> None:
    rig = _build(_overlay(realm="other-realm"))
    result = asyncio.run(
        rig.handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    assert "realm" in result.detail
    assert "other-realm" in result.detail
    _assert_zero_calls(rig)


def test_a_probe_with_an_undeclared_lane_is_refused_too() -> None:
    rig = _build(_overlay())
    result = asyncio.run(
        rig.handler.handle(
            ModelDevIdentityProvisionCommand(lane="staging", write=False)
        )
    )
    assert result.ok is False
    assert result.outcome.value == "refused_not_dev"
    _assert_zero_calls(rig)


def test_failing_control_a_contract_declaring_the_other_lane_runs_the_flow(
    tmp_path: Path,
) -> None:
    """A contract file declaring another lane and realm lets the same handler run the flow."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["dev_only"]["lane"] = "staging"
    contract["dev_only"]["realms"] = ["other-realm"]
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")

    rig = _build(
        _overlay(lane="staging", realm="other-realm"), contract_path=contract_path
    )
    result = asyncio.run(
        rig.handler.handle(ModelDevIdentityProvisionCommand(lane="staging", write=True))
    )
    assert result.ok is True
    assert result.outcome.value == "provisioned"
    assert len(rig.transport.call_log) == 5


def test_the_declared_lane_and_realms_are_the_contract_file_s(tmp_path: Path) -> None:
    """The guard's lane and realms come from the contract file, not from code."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["dev_only"] == {"lane": "dev", "realms": ["omninode"]}

    # And the guard honors a changed file: a dev command against a staging-only contract is refused.
    contract["dev_only"]["lane"] = "staging"
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")
    rig = _build(_overlay(), contract_path=contract_path)
    result = asyncio.run(
        rig.handler.handle(ModelDevIdentityProvisionCommand(lane="dev", write=True))
    )
    assert result.outcome.value == "refused_not_dev"
    _assert_zero_calls(rig)
