# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""POST /v1/tenants/me/inference-credentials through the mounted app.

Ports the behaviour the hosted API's route tests pin: a created credential comes
back as a reference and metadata, never the key; a failing service is a 503; a
422 never echoes what the caller submitted.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from tests.nodes.inference_credential_http_support import (
    GOOD_BODY,
    IDEMPOTENCY,
    ROUTE,
    TENANT_A,
    World,
    undecided_plan,
)

pytestmark = pytest.mark.unit


def _sentinel() -> str:
    return f"SENTINEL-not-a-real-key-{uuid.uuid4().hex}"


class TestCreate:
    def test_create_returns_ref_and_never_leaks_key_value(self) -> None:
        world = World()
        response = world.client.post(
            ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["api_key_ref"].startswith(f"cred_{TENANT_A}_openai_")
        assert body["name"] == "my key"
        assert body["provider"] == "openai"
        assert "key_value" not in body
        assert GOOD_BODY["key_value"] not in response.text
        # The key reaches the store under the ref and nowhere else.
        assert world.store.values == {body["api_key_ref"]: GOOD_BODY["key_value"]}
        [(topic, key, envelope)] = world.bus.published
        assert topic == "onex.evt.omnimarket.credential-registered.v1"
        assert key == body["api_key_ref"].encode()
        assert envelope.payload.tenant_id == str(TENANT_A)
        assert GOOD_BODY["key_value"] not in envelope.model_dump_json()

    def test_service_failure_returns_503(self) -> None:
        world = World()
        world.store.set_error = RuntimeError("store unavailable")
        response = world.client.post(
            ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
        )
        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "failed to register inference credential"}
        assert world.bus.published == []

    def test_a_caller_supplied_tenant_or_ref_is_refused_not_honoured(self) -> None:
        world = World()
        for extra in ({"tenant_id": str(uuid.uuid4())}, {"api_key_ref": "cred_x"}):
            response = world.client.post(
                ROUTE,
                json=GOOD_BODY | extra,
                headers=world.as_tenant_a(**IDEMPOTENCY),
            )
            assert response.status_code == 422, response.text
        assert world.adapter_calls() == 0

    def test_a_key_the_provider_refuses_is_a_typed_422(self) -> None:
        world = World()
        from omnimarket.handlers.inference_credentials_http.bindings import (
            bind_inference_credential_ports,
        )

        bind_inference_credential_ports(plan_detector=undecided_plan)
        response = world.client.post(
            ROUTE,
            json={
                "name": "n",
                "provider": "glm",
                "model": "glm-4.5-flash",
                "key_value": "fixture-key-x",
            },
            headers=world.as_tenant_a(**IDEMPOTENCY),
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "BYOK_PLAN_UNDETERMINED"
        assert world.store.calls == []
        assert world.bus.published == []


class TestIdempotency:
    def test_the_same_key_and_body_converge_on_one_secret(self) -> None:
        world = World()
        first = world.client.post(
            ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
        )
        second = world.client.post(
            ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["api_key_ref"] == second.json()["api_key_ref"]
        assert len(world.store.values) == 1

    def test_another_key_or_body_or_tenant_gets_another_ref(self) -> None:
        world = World()
        refs = {
            world.client.post(
                ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
            ).json()["api_key_ref"],
            world.client.post(
                ROUTE,
                json=GOOD_BODY,
                headers=world.as_tenant_a(**{"Idempotency-Key": "idem-2"}),
            ).json()["api_key_ref"],
            world.client.post(
                ROUTE,
                json=GOOD_BODY | {"name": "other"},
                headers=world.as_tenant_a(**IDEMPOTENCY),
            ).json()["api_key_ref"],
            world.client.post(
                ROUTE, json=GOOD_BODY, headers=world.as_tenant_b(**IDEMPOTENCY)
            ).json()["api_key_ref"],
        }
        assert len(refs) == 4

    def test_a_missing_idempotency_key_is_refused_before_any_write(self) -> None:
        world = World()
        response = world.client.post(ROUTE, json=GOOD_BODY, headers=world.as_tenant_a())
        assert response.status_code == 400, response.text
        assert world.adapter_calls() == 0


class TestValidationErrorDoesNotEchoTheKey:
    def _post(self, world: World, **kwargs: Any) -> Any:
        return world.client.post(
            ROUTE, headers=world.as_tenant_a(**IDEMPOTENCY), **kwargs
        )

    def test_misnamed_key_field_is_not_reflected(self) -> None:
        sentinel = _sentinel()
        world = World()
        response = self._post(
            world, json={"name": "my key", "provider": "openai", "key": sentinel}
        )
        assert response.status_code == 422, response.text
        assert sentinel not in response.text

    def test_key_value_present_but_another_field_invalid(self) -> None:
        sentinel = _sentinel()
        world = World()
        response = self._post(
            world,
            json={
                "name": "my key",
                "provider": "not a valid provider",
                "key_value": sentinel,
            },
        )
        assert response.status_code == 422, response.text
        assert sentinel not in response.text

    def test_key_submitted_as_a_field_name_is_not_reflected(self) -> None:
        sentinel = _sentinel()
        world = World()
        response = self._post(world, json=GOOD_BODY | {sentinel: "x"})
        assert response.status_code == 422, response.text
        assert sentinel not in response.text

    def test_unparseable_body_is_not_reflected(self) -> None:
        sentinel = _sentinel()
        world = World()
        response = self._post(
            world,
            content=b'{"name": "my key", "key_value": "' + sentinel.encode() + b'"',
        )
        assert response.status_code == 422, response.text
        assert sentinel not in response.text

    def test_wrong_type_for_key_value_is_not_reflected(self) -> None:
        sentinel = _sentinel()
        world = World()
        response = self._post(world, json=GOOD_BODY | {"key_value": [sentinel]})
        assert response.status_code == 422, response.text
        assert sentinel not in response.text

    def test_no_log_record_carries_the_submitted_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sentinel = _sentinel()
        world = World()
        with caplog.at_level(logging.DEBUG):
            response = self._post(
                world, json={"name": "my key", "provider": "openai", "key": sentinel}
            )
        assert response.status_code == 422
        for record in caplog.records:
            assert sentinel not in record.getMessage()
            assert sentinel not in str(record.args)

    def test_the_refusal_stays_actionable(self) -> None:
        world = World()
        response = self._post(
            world, json={"provider": "openai", "key_value": "placeholder"}
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert ["body", "name"] in [error["loc"] for error in detail]
        for error in detail:
            assert "input" not in error
            assert "ctx" not in error
            assert {"type", "loc", "msg"} <= set(error)
