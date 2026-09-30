# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: the hosted path records a credential's plan and routes on it.

Intake decides the plan (the customer's, or detection's) BEFORE the key is
stored, the event carries it on ``metadata``, and the projection mints the
overlay route for THAT plan's endpoint. The overlay row records the plan through
the ``backend_id`` it is minted with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import ValidationError

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.credential_publisher import (
    CredentialPlanUndeterminedError,
    ModelCredentialRegisteredEvent,
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.routing.byok_plan_detection import ModelByokPlanDetection
from omnimarket.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
    resolve_byok_backend_by_id,
)

pytestmark = pytest.mark.unit

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
_KEY = "zai-hosted-key-do-not-leak-0123456789"  # onex-allow-test-fixture OMN-20157 reason="synthetic key literal asserted absent from every event and message"


def _request(**overrides: object) -> ModelInferenceCredentialCreateRequest:
    fields: dict[str, object] = {"name": "k", "provider": "glm", "key_value": _KEY}
    fields.update(overrides)
    return ModelInferenceCredentialCreateRequest(**fields)


def _detector(plan: str | None, outcome: str = "detected") -> AsyncMock:
    return AsyncMock(
        return_value=ModelByokPlanDetection(
            provider="glm",
            plan=plan,
            outcome=outcome,  # type: ignore[arg-type]
        )
    )


def _coding_only_detector() -> AsyncMock:
    """What detection returns for a key only the Coding Plan surface answered."""
    return AsyncMock(
        return_value=ModelByokPlanDetection(
            provider="glm",
            plan=None,
            outcome="not_permitted",
            refused_plan="coding_plan",
            refusal_code="BYOK_CODING_PLAN_NOT_PERMITTED",
        )
    )


class TestRequestPlan:
    def test_a_named_plan_of_the_provider_is_accepted(self) -> None:
        assert _request(plan="general_api").plan == "general_api"

    def test_a_plan_the_provider_does_not_declare_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not a plan of provider"):
            _request(plan="enterprise")

    def test_a_plan_of_another_provider_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not a plan of provider"):
            _request(provider="gemini", plan="coding_plan")

    def test_gemini_is_now_accepted_at_intake(self) -> None:
        assert _request(provider="gemini").provider == "gemini"

    def test_a_plan_is_optional(self) -> None:
        assert _request().plan is None


class TestRegisterDecidesThePlanFirst:
    async def test_a_named_plan_skips_detection_and_rides_the_event(self) -> None:
        detector = _detector("general_api")
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        resp = await register_inference_credential(
            _request(plan="general_api"),
            tenant_id="t1",
            secret_store=store,
            event_bus=bus,
            plan_detector=detector,
        )

        detector.assert_not_awaited()
        assert resp.plan == "general_api"
        payload = bus.publish_envelope.await_args.args[0].payload
        assert isinstance(payload, ModelCredentialRegisteredEvent)
        assert payload.metadata == {"plan": "general_api"}

    async def test_an_omitted_plan_is_detected_and_recorded(self) -> None:
        detector = _detector("general_api")
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        resp = await register_inference_credential(
            _request(),
            tenant_id="t1",
            secret_store=store,
            event_bus=bus,
            plan_detector=detector,
        )

        detector.assert_awaited_once()
        assert resp.plan == "general_api"
        payload = bus.publish_envelope.await_args.args[0].payload
        assert payload.metadata == {"plan": "general_api"}

    async def test_a_coding_plan_only_key_is_refused_with_the_typed_code(
        self,
    ) -> None:
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        with pytest.raises(ByokPlanNotPermittedError) as excinfo:
            await register_inference_credential(
                _request(),
                tenant_id="t1",
                secret_store=store,
                event_bus=bus,
                plan_detector=_coding_only_detector(),
            )

        store.set_secret.assert_not_awaited()
        bus.publish_envelope.assert_not_awaited()
        assert excinfo.value.code == "BYOK_CODING_PLAN_NOT_PERMITTED"
        assert "general API key" in str(excinfo.value)
        assert _KEY not in str(excinfo.value)

    async def test_a_named_coding_plan_is_refused_before_any_network_probe(
        self,
    ) -> None:
        detector = _detector("coding_plan")
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        with pytest.raises(ByokPlanNotPermittedError) as excinfo:
            await register_inference_credential(
                _request(plan="coding_plan"),
                tenant_id="t1",
                secret_store=store,
                event_bus=bus,
                plan_detector=detector,
            )

        detector.assert_not_awaited()
        store.set_secret.assert_not_awaited()
        bus.publish_envelope.assert_not_awaited()
        assert excinfo.value.code == "BYOK_CODING_PLAN_NOT_PERMITTED"

    async def test_a_detector_that_names_the_coding_plan_is_still_refused(
        self,
    ) -> None:
        # Defence in depth: whatever a detector returns, a plan the catalogue
        # declares detection-only is never filed.
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        with pytest.raises(ByokPlanNotPermittedError):
            await register_inference_credential(
                _request(),
                tenant_id="t1",
                secret_store=store,
                event_bus=bus,
                plan_detector=_detector("coding_plan"),
            )

        store.set_secret.assert_not_awaited()
        bus.publish_envelope.assert_not_awaited()

    @pytest.mark.parametrize("outcome", ["rejected", "inconclusive", "ambiguous"])
    async def test_an_undecided_plan_stores_and_publishes_nothing(
        self, outcome: str
    ) -> None:
        store = AsyncMock()
        bus = AsyncMock(spec=EventBusKafka)

        with pytest.raises(CredentialPlanUndeterminedError) as excinfo:
            await register_inference_credential(
                _request(),
                tenant_id="t1",
                secret_store=store,
                event_bus=bus,
                plan_detector=_detector(None, outcome),
            )

        store.set_secret.assert_not_awaited()
        bus.publish_envelope.assert_not_awaited()
        assert excinfo.value.outcome == outcome
        # Only a plan a customer may register under is offered back.
        assert excinfo.value.plans == ("general_api",)
        assert _KEY not in str(excinfo.value)

    async def test_a_single_plan_provider_is_never_detected_and_records_no_plan(
        self,
    ) -> None:
        detector = _detector("nope")
        bus = AsyncMock(spec=EventBusKafka)

        resp = await register_inference_credential(
            _request(provider="gemini"),
            tenant_id="t1",
            secret_store=AsyncMock(),
            event_bus=bus,
            plan_detector=detector,
        )

        detector.assert_not_awaited()
        assert resp.plan is None
        assert bus.publish_envelope.await_args.args[0].payload.metadata == {}


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    return db


@pytest.fixture
def runner(mock_db: AsyncMock) -> HandlerTenantCredentialsProjectionRunner:
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = mock_db
    return r


def _overlay_args(mock_db: AsyncMock) -> tuple[object, ...]:
    calls = [
        c
        for c in mock_db.execute.await_args_list
        if "INSERT INTO delegation_routing_tenant_overlay" in str(c.args[0])
    ]
    assert len(calls) == 1
    return tuple(calls[0].args)


async def _project(
    runner: HandlerTenantCredentialsProjectionRunner,
    provider: str,
    metadata: dict[str, str] | None,
) -> None:
    data: dict[str, object] = {
        "tenant_id": "t1",
        "provider": provider,
        "name": "k",
        "api_key_ref": f"cred_t1_{provider}_abc",
    }
    if metadata is not None:
        data["metadata"] = metadata
    await runner.project_event(
        TOPIC_REGISTERED, data, MessageMeta(partition=0, offset=0, fallback_id="f")
    )


class TestProjectionRoutesOnThePlan:
    async def test_a_general_api_credential_mints_the_general_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await _project(runner, "glm", {"plan": "general_api"})
        args = _overlay_args(mock_db)
        # (sql, tenant, task_type, backend_id, provider, endpoint, model, ref, ...)
        assert args[3] == "byok-glm-general"
        assert args[4] == "glm"
        assert args[5] == "https://api.z.ai/api/paas/v4/chat/completions"
        assert args[6] == "glm-4.5-flash"
        assert args[7] == "cred_t1_glm_abc"

    async def test_a_coding_plan_credential_mints_no_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        # OMN-20157: never route a customer to the Coding Plan endpoint, even
        # for an event a stale client published naming it.
        await _project(runner, "glm", {"plan": "coding_plan"})
        overlay = [
            c
            for c in mock_db.execute.await_args_list
            if "INSERT INTO delegation_routing_tenant_overlay" in str(c.args[0])
        ]
        assert overlay == []

    async def test_an_event_from_before_plans_resolves_the_general_api(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await _project(runner, "glm", None)
        args = _overlay_args(mock_db)
        assert args[3] == "byok-glm-general"
        assert args[5] == "https://api.z.ai/api/paas/v4/chat/completions"

    async def test_an_undeclared_plan_mints_no_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await _project(runner, "glm", {"plan": "enterprise"})
        overlay = [
            c
            for c in mock_db.execute.await_args_list
            if "INSERT INTO delegation_routing_tenant_overlay" in str(c.args[0])
        ]
        assert overlay == []

    async def test_a_gemini_credential_mints_the_gemini_route(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        await _project(runner, "gemini", None)
        args = _overlay_args(mock_db)
        assert args[3] == "byok-gemini"
        assert args[6] == "gemini-2.5-flash-lite"

    async def test_the_plan_is_readable_back_off_the_overlay_backend_id(self) -> None:
        row = resolve_byok_backend_by_id("byok-glm-general")
        assert row is not None
        assert (row.provider, row.plan) == ("glm", "general_api")
