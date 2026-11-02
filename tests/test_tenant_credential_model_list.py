"""RED-first contract tests for hosted BYOK model-list intake (OMN-21217 T5)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.routing.byok_model_discovery import ModelByokModelDiscovery
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)


def _request(**overrides: object) -> ModelInferenceCredentialCreateRequest:
    fields: dict[str, object] = {
        "name": "tenant-key",
        "provider": "openrouter",
        "key_value": SecretStr("sk-test-model-list-only"),
    }
    fields.update(overrides)
    return ModelInferenceCredentialCreateRequest(**fields)


def test_create_request_preserves_the_customer_model_order() -> None:
    request = _request(models=["vendor/model-a", "vendor/model-b"])

    assert request.models == ["vendor/model-a", "vendor/model-b"]


def test_single_model_alias_remains_supported_and_cannot_mix_with_models() -> None:
    request = _request(model="vendor/model-a")

    assert request.model == "vendor/model-a"
    assert request.models == ["vendor/model-a"]
    with pytest.raises(ValidationError):
        _request(model="vendor/model-a", models=["vendor/model-b"])


@pytest.mark.parametrize(
    "models", [[], ["vendor/model-a", "vendor/model-a"], [""], ["  "], ["x" * 201]]
)
def test_create_request_rejects_empty_or_duplicate_model_lists(
    models: list[str],
) -> None:
    with pytest.raises(ValidationError):
        _request(models=models)


@pytest.mark.parametrize("version", [0, -1, True, "1"])
def test_expected_version_is_a_positive_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        _request(models=["vendor/model-a"], expected_version=version)


@pytest.mark.asyncio
async def test_intake_checks_every_model_and_publishes_order_and_version() -> None:
    """A checked first model must not smuggle an unchecked second choice."""
    store, bus = AsyncMock(), AsyncMock()

    async def discover(
        backend: object, key: SecretStr, *, chosen: str
    ) -> ModelByokModelDiscovery:
        return ModelByokModelDiscovery(
            provider="openrouter", plan="default", outcome="resolved", model=chosen
        )

    discovery = AsyncMock(side_effect=discover)
    result = await register_inference_credential(
        _request(models=["vendor/model-b", "vendor/model-a"], expected_version=3),
        tenant_id="tenant-a",
        secret_store=store,
        event_bus=bus,
        model_discoverer=discovery,
    )
    assert [call.kwargs["chosen"] for call in discovery.await_args_list] == [
        "vendor/model-b",
        "vendor/model-a",
    ]
    event = bus.publish_envelope.await_args.args[0].payload
    assert event.models == result.models == ["vendor/model-b", "vendor/model-a"]
    assert event.expected_version == 3
    assert event.metadata["model"] == "vendor/model-b"
    assert "sk-test-model-list-only" not in event.model_dump_json()
    store.set_secret.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_second_model_refuses_before_storing_or_publishing() -> None:
    store, bus = AsyncMock(), AsyncMock()
    discovery = AsyncMock(
        side_effect=[
            ModelByokModelDiscovery(
                provider="openrouter",
                plan="default",
                outcome="resolved",
                model="vendor/model-a",
            ),
            ModelByokModelDiscovery(
                provider="openrouter",
                plan="default",
                outcome="not_listed",
                chosen="vendor/model-b",
            ),
        ]
    )
    with pytest.raises(CredentialKeyRefusedError):
        await register_inference_credential(
            _request(models=["vendor/model-a", "vendor/model-b"]),
            tenant_id="tenant-a",
            secret_store=store,
            event_bus=bus,
            model_discoverer=discovery,
        )
    store.set_secret.assert_not_awaited()
    bus.publish_envelope.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_choice_remains_a_typed_not_chosen_refusal() -> None:
    store, bus = AsyncMock(), AsyncMock()
    with pytest.raises(CredentialKeyRefusedError) as exc:
        await register_inference_credential(
            _request(), tenant_id="tenant-a", secret_store=store, event_bus=bus
        )
    assert exc.value.outcome == "not_chosen"
    store.set_secret.assert_not_awaited()
    bus.publish_envelope.assert_not_awaited()
