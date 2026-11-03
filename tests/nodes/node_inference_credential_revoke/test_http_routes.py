# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DELETE /v1/tenants/me/inference-credentials/{api_key_ref} through the mounted app."""

from __future__ import annotations

import pytest

from omnimarket.handlers.inference_credentials_http.errors import (
    CredentialReadModelMissingTableError,
)
from tests.nodes.inference_credential_http_support import (
    ROUTE,
    TENANT_A,
    TENANT_B,
    World,
    fake_row,
)

pytestmark = pytest.mark.unit


class TestRevokeCrossTenantAuthz:
    def test_tenant_a_cannot_revoke_tenant_bs_credential(self) -> None:
        victim = fake_row(TENANT_B, "victim")
        world = World(rows=[victim])
        response = world.client.delete(
            f"{ROUTE}/{victim['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "inference credential not found"}
        assert world.read_model.calls == [
            ("owned", victim["api_key_ref"], str(TENANT_A))
        ]
        assert world.store.calls == []
        assert world.bus.published == []
        assert world.outbox.records == []

    def test_not_owned_and_not_found_are_indistinguishable(self) -> None:
        victim = fake_row(TENANT_B, "victim")
        world = World(rows=[victim])
        owned_by_other = world.client.delete(
            f"{ROUTE}/{victim['api_key_ref']}", headers=world.as_tenant_a()
        )
        absent = world.client.delete(
            f"{ROUTE}/cred_nothing_here", headers=world.as_tenant_a()
        )
        assert owned_by_other.status_code == absent.status_code == 404
        assert owned_by_other.content == absent.content

    def test_tenant_revoking_its_own_credential_succeeds(self) -> None:
        own = fake_row(TENANT_A, "mine")
        world = World(rows=[own])
        world.store.values[own["api_key_ref"]] = "fixture-held-secret-value"
        response = world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "api_key_ref": own["api_key_ref"],
            "status": "revocation-published",
        }
        assert own["api_key_ref"] not in world.store.values
        [(topic, key, envelope)] = world.bus.published
        assert topic == "onex.evt.omnimarket.credential-revoked.v1"
        assert key == own["api_key_ref"].encode()
        assert envelope.payload.tenant_id == str(TENANT_A)

    def test_a_repeat_delete_answers_200_again(self) -> None:
        own = fake_row(TENANT_A, "mine", revoked=True)
        world = World(rows=[own])
        first = world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        second = world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert first.status_code == second.status_code == 200

    def test_the_outbox_row_is_written_before_the_store_delete(self) -> None:
        own = fake_row(TENANT_A, "mine")
        world = World(rows=[own])
        world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert world.outbox.records == [(own["api_key_ref"], str(TENANT_A))]
        assert world.outbox.deletes_seen_at_record == [0]


class TestRevokeStillFailsClosed:
    def test_a_missing_relation_on_delete_does_not_degrade(self) -> None:
        own = fake_row(TENANT_A, "mine")
        world = World(rows=[own])
        world.read_model.error = CredentialReadModelMissingTableError("absent")
        response = world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert response.status_code == 500, response.text
        assert world.store.calls == []
        assert world.bus.published == []

    def test_an_unbound_read_model_fails_closed(self) -> None:
        from omnimarket.handlers.inference_credentials_http.bindings import (
            bind_inference_credential_ports,
        )

        world = World()
        bind_inference_credential_ports(read_model=None)
        response = world.client.delete(f"{ROUTE}/cred_x", headers=world.as_tenant_a())
        assert response.status_code == 500
        assert world.store.calls == []
        assert world.bus.published == []

    def test_a_store_failure_publishes_nothing(self) -> None:
        own = fake_row(TENANT_A, "mine")
        world = World(rows=[own])

        world.store.delete_error = RuntimeError("store down")
        response = world.client.delete(
            f"{ROUTE}/{own['api_key_ref']}", headers=world.as_tenant_a()
        )
        assert response.status_code == 503, response.text
        assert world.bus.published == []

    def test_an_over_long_ref_is_a_422(self) -> None:
        world = World()
        response = world.client.delete(
            f"{ROUTE}/{'x' * 301}", headers=world.as_tenant_a()
        )
        assert response.status_code == 422
        assert world.adapter_calls() == 0
