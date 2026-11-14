# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sign-in client's tenant claim mappers (OMN-24304): golden, control, guard and error chain.

The overlay declares one sign-in client and the mappers its access tokens need; a run creates each
mapper on that client with the same admin token the rest of the run uses, a second run finds the
mappers by name and PUTs them in place (no duplicates), and every break is one typed outcome. The
overlay is the node's own overlay.example.yaml, so a renamed overlay key or mapper fails here too.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dev_keycloak_client_effect.handlers import (
    HandlerDevKeycloakClient,
    ModelDevKeycloakClientCommand,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_dev_keycloak_client import (
    ModelDevKeycloakClientResult,
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

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
OVERLAY_EXAMPLE = NODE_DIR / "overlay.example.yaml"

_SECRET = "Gk7!dev-client-secret"
_ADMIN_PASSWORD = "bootstrap-password-value"
_ADMIN_TOKEN = "admin-access-token"
_CLIENT_TOKEN = "client-access-token"
_COMMAND = ModelDevKeycloakClientCommand(lane="dev", write=True)


def _document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        OVERLAY_EXAMPLE.read_text(encoding="utf-8")
    )
    return document


def _overlay(
    document: dict[str, Any] | None = None,
) -> ModelDevKeycloakClientOverlay:
    return ModelDevKeycloakClientOverlay.model_validate(document or _document())


def _declared_bodies() -> list[dict[str, Any]]:
    """The mapper representations the overlay declares, as the node must send them."""
    return [
        {
            "name": mapper["name"],
            "protocol": "openid-connect",
            "protocolMapper": mapper["protocol_mapper"],
            "config": dict(mapper["config"]),
        }
        for mapper in _document()["sign_in_client"]["mappers"]
    ]


class _Rig:
    def __init__(
        self,
        *,
        overlay: ModelDevKeycloakClientOverlay | None = None,
        **fake_options: Any,
    ) -> None:
        self.overlay = overlay or _overlay()
        self.transport = FakeKeycloak(**fake_options)
        self.store = FakeSettingStore()
        self.store.set("/shared/env", "KEYCLOAK_DEV_CLIENT_SECRET", _SECRET)
        self.gen = FakeKeyGenerator(outcome="created", value=_SECRET)
        self.credentials = FakeCredentialSource(
            {
                "ADMIN_USERNAME": "bootstrap-admin",
                "ADMIN_PASSWORD": _ADMIN_PASSWORD,
            }
        )
        self.handler = HandlerDevKeycloakClient(
            overlay=self.overlay,
            transport=self.transport,
            settings_store=self.store,
            key_generator=self.gen,
            credential_source=self.credentials,
            contract_path=CONTRACT_PATH,
        )

    def run(self) -> ModelDevKeycloakClientResult:
        return asyncio.run(self.handler.handle(_COMMAND))

    def client_token_requests(self) -> list[str]:
        """The client_credentials proof requests: token calls on the realm, not the admin realm."""
        return [
            call
            for call in self.transport.call_log
            if call.startswith("POST ")
            and "/realms/omninode/protocol/openid-connect/token" in call
        ]

    def sign_in_mappers(self) -> list[dict[str, Any]]:
        uuid = self.transport.sign_in_uuid
        assert uuid is not None
        return self.transport.mappers_by_client.get(uuid, [])


def _assert_secretless(result: ModelDevKeycloakClientResult, logs: str) -> None:
    for text in (result.model_dump_json(), repr(result), str(result), logs):
        for secret in (_SECRET, _ADMIN_PASSWORD, _ADMIN_TOKEN, _CLIENT_TOKEN):
            assert secret not in text


def test_golden_first_run_creates_exactly_the_declared_mappers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = _Rig()

    result = rig.run()

    assert result.ok is True
    assert result.outcome.value == "ensured"
    assert result.mappers_ensured == 2
    assert rig.transport.mapper_created_bodies == _declared_bodies()
    assert rig.transport.mapper_updated_bodies == []
    assert [mapper["name"] for mapper in rig.sign_in_mappers()] == [
        "tenant_id",
        "tenant_slug",
    ]
    # The mapper calls ride the same admin bearer as every other admin call.
    mapper_calls = [
        request
        for request in rig.transport.sent
        if "/protocol-mappers/models" in request.url
    ]
    assert len(mapper_calls) == 3
    assert all(
        request.bearer is not None and request.bearer.get_secret_value() == _ADMIN_TOKEN
        for request in mapper_calls
    )
    _assert_secretless(result, caplog.text)


def test_idempotent_second_run_updates_the_mappers_without_duplicates() -> None:
    rig = _Rig()

    first = rig.run()
    second = rig.run()

    assert first.ok is True
    assert second.ok is True
    assert second.mappers_ensured == 2
    assert len(rig.transport.mapper_created_bodies) == 2
    # Each PUT carries the declared representation plus the id the first run's create was given.
    assert [
        {key: value for key, value in body.items() if key != "id"}
        for body in rig.transport.mapper_updated_bodies
    ] == _declared_bodies()
    assert [body["id"] for body in rig.transport.mapper_updated_bodies] == [
        "mapper-uuid-0001",
        "mapper-uuid-0002",
    ]
    assert [mapper["name"] for mapper in rig.sign_in_mappers()] == [
        "tenant_id",
        "tenant_slug",
    ]


def test_failing_control_a_run_without_the_mapper_step_leaves_the_client_without_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The golden assertions depend on the new step: with it replaced by a no-op they cannot hold."""

    async def _no_op(self: HandlerDevKeycloakClient, admin_token: object) -> None:
        return None

    monkeypatch.setattr(HandlerDevKeycloakClient, "_ensure_mappers", _no_op)
    rig = _Rig()

    result = rig.run()

    assert result.outcome.value == "ensured"
    assert rig.sign_in_mappers() == []
    assert rig.transport.mapper_created_bodies != _declared_bodies()


def test_a_sign_in_client_outside_the_contract_is_refused_before_any_call() -> None:
    document = _document()
    document["sign_in_client"]["client_id"] = "rogue-spa"
    rig = _Rig(overlay=_overlay(document), sign_in_client_id="rogue-spa")

    result = rig.run()

    assert result.ok is False
    assert result.outcome.value == "refused_mapper_client_not_allowed"
    assert "rogue-spa" in result.detail
    assert rig.transport.call_log == []
    assert rig.credentials.calls == []
    assert rig.store.calls == []
    assert rig.gen.calls == []


def test_sign_in_client_not_found_is_typed_and_no_proof_request_is_made() -> None:
    rig = _Rig(sign_in_client_exists=False)

    result = rig.run()

    assert result.ok is False
    assert result.outcome.value == "mapper_client_not_found"
    assert result.client_id == "omnidash-spa"
    assert "omnidash-spa" in result.detail
    assert rig.client_token_requests() == []
    assert rig.transport.mapper_created_bodies == []
    _assert_secretless(result, "")


def test_mapper_lookup_failed_is_typed() -> None:
    rig = _Rig(fail_mapper_lookup=True)

    result = rig.run()

    assert result.ok is False
    assert result.outcome.value == "mapper_write_failed"
    assert "500" in result.detail
    assert rig.client_token_requests() == []


def test_mapper_create_failed_names_the_mapper() -> None:
    rig = _Rig(fail_mapper_create=True)

    result = rig.run()

    assert result.ok is False
    assert result.outcome.value == "mapper_write_failed"
    assert "tenant_id" in result.detail
    assert "500" in result.detail
    assert rig.client_token_requests() == []
    _assert_secretless(result, "")


def test_mapper_update_failed_names_the_mapper() -> None:
    first = _Rig()
    assert first.run().ok is True
    # A second rig that starts from the first one's realm state: the mappers already exist.
    second = _Rig(fail_mapper_update=True)
    second.transport = first.transport
    second.transport.fail_mapper_update = True
    second.handler = HandlerDevKeycloakClient(
        overlay=second.overlay,
        transport=second.transport,
        settings_store=second.store,
        key_generator=second.gen,
        credential_source=second.credentials,
        contract_path=CONTRACT_PATH,
    )

    result = second.run()

    assert result.ok is False
    assert result.outcome.value == "mapper_write_failed"
    assert "tenant_id" in result.detail
    assert "update" in result.detail


def test_connection_error_on_the_mapper_calls_is_keycloak_unreachable() -> None:
    rig = _Rig(
        raise_connection_error=True, raise_connection_error_on="/protocol-mappers/"
    )

    result = rig.run()

    assert result.ok is False
    assert result.outcome.value == "keycloak_unreachable"
    assert result.client_id == "omnidash-spa"
    assert rig.client_token_requests() == []
