# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the BYOK credential-intake thin publisher (OMN-16316).

Covers the ticket's hard security constraints:
- the key value never appears in any log record produced during a register call
- the published credential-registered event structurally cannot carry a
  value/key_value field (extra="forbid" + explicit field-set assertion)
- api_key_ref is minted (tenant-scoped, collision-safe), never caller-supplied
- set_secret is called with the minted ref + the submitted value, exactly once
- the response body never contains the value
- revoke never calls delete_secret, and publishes credential-revoked
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import SecretStr, ValidationError

from omnimarket.events.topics import (
    CREDENTIAL_REGISTERED_TOPIC_V1,
    CREDENTIAL_REVOKED_TOPIC_V1,
)
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
    ModelInferenceCredentialCreateRequest,
    ModelInferenceCredentialResponse,
    ModelInferenceCredentialRevokeResponse,
    mint_api_key_ref,
    register_inference_credential,
    revoke_inference_credential,
)

pytestmark = pytest.mark.unit

_SECRET_VALUE = "sk-super-secret-do-not-leak-abc123"  # onex-allow-test-fixture OMN-16316 reason="synthetic BYOK key literal asserted to never appear in any response/event/log, not a real credential"


def _make_request(**overrides: object) -> ModelInferenceCredentialCreateRequest:
    fields: dict[str, object] = {
        "name": "my-openrouter-key",
        "provider": "openrouter",
        "key_value": _SECRET_VALUE,
    }
    fields.update(overrides)
    return ModelInferenceCredentialCreateRequest(**fields)


def test_topics_are_canonical_evt_shape() -> None:
    assert (
        CREDENTIAL_REGISTERED_TOPIC_V1 == "onex.evt.omnimarket.credential-registered.v1"
    )
    assert CREDENTIAL_REVOKED_TOPIC_V1 == "onex.evt.omnimarket.credential-revoked.v1"


def test_mint_api_key_ref_is_tenant_scoped_and_collision_safe() -> None:
    a = mint_api_key_ref("tenant-1", "openrouter")
    b = mint_api_key_ref("tenant-1", "openrouter")
    assert a != b
    assert a.startswith("cred_tenant-1_openrouter_")
    assert "tenant-1" in a


def test_create_request_key_value_is_secret_str() -> None:
    req = _make_request()
    assert isinstance(req.key_value, SecretStr)
    # repr/str must never leak the raw value — this is the class-level guard
    # pydantic.SecretStr provides; asserted here so a future field-type
    # change (e.g. someone "simplifying" to a plain str) fails this test.
    assert _SECRET_VALUE not in repr(req)
    assert _SECRET_VALUE not in str(req)


def test_create_request_rejects_empty_name_and_provider() -> None:
    with pytest.raises(ValidationError):
        _make_request(name="")
    with pytest.raises(ValidationError):
        _make_request(provider="")


@pytest.mark.parametrize(
    "bad_provider",
    [
        "open router",  # whitespace
        "openrouter\n",  # control byte (newline)
        "openrouter\t",  # control byte (tab)
        "../openrouter",  # path traversal via mint_api_key_ref -> Infisical path
        "openrouter/../other-tenant",  # path traversal
        "openrouter\x00",  # NUL byte
    ],
)
def test_create_request_rejects_unsafe_provider_charset(bad_provider: str) -> None:
    """``provider`` is interpolated unencoded into ``mint_api_key_ref``'s
    output, which becomes both the Infisical secret path segment and the
    Kafka message key -- whitespace, control bytes, or path separators must
    be rejected at the request boundary, not reach either downstream
    identifier (CodeRabbit finding, omnimarket#2117)."""
    with pytest.raises(ValidationError):
        _make_request(provider=bad_provider)


def test_create_request_charset_pattern_admits_hyphen_and_underscore() -> None:
    """The safe-charset pattern must not regress legitimate provider ids.

    Since OMN-17939 the field carries a SECOND constraint -- membership in
    ``customer_provider_catalogue()`` -- so a well-formed id is no longer
    accepted outright, and asserting acceptance would now be asserting the
    catalogue's contents rather than the charset. The original guarantee is
    pinned directly instead: a hyphen/underscore id must get past the PATTERN
    and be refused only on membership. Pydantic discriminates the two --
    ``string_pattern_mismatch`` vs the validator's ``value_error`` -- so this
    fails if the charset ever narrows to exclude ``-`` or ``_``.
    """
    with pytest.raises(ValidationError) as excinfo:
        _make_request(provider="open-router_v2")

    error_types = {error["type"] for error in excinfo.value.errors()}
    assert error_types == {"value_error"}, (
        "'open-router_v2' was refused by the charset pattern, not by the "
        f"catalogue check; the safe-charset pattern has regressed: {error_types}"
    )


def test_credential_registered_event_never_carries_a_secret_field() -> None:
    """Structural guard: the event model's field set can never include a
    value/key_value field, and extra="forbid" means any accidental attempt
    to construct it with one raises immediately."""
    allowed_fields = {"tenant_id", "provider", "name", "api_key_ref", "metadata"}
    assert set(ModelCredentialRegisteredEvent.model_fields) == allowed_fields

    with pytest.raises(ValidationError):
        ModelCredentialRegisteredEvent(
            tenant_id="t1",
            provider="openrouter",
            name="k1",
            api_key_ref="cred_t1_openrouter_x",
            key_value=_SECRET_VALUE,  # type: ignore[call-arg]
        )


def test_credential_revoked_event_never_carries_a_secret_field() -> None:
    allowed_fields = {"tenant_id", "api_key_ref"}
    assert set(ModelCredentialRevokedEvent.model_fields) == allowed_fields


@pytest.mark.asyncio
async def test_register_calls_set_secret_with_minted_ref_and_submitted_value() -> None:
    secret_store = AsyncMock()
    event_bus = AsyncMock(spec=EventBusKafka)
    req = _make_request()

    resp = await register_inference_credential(
        req, tenant_id="tenant-42", secret_store=secret_store, event_bus=event_bus
    )

    secret_store.set_secret.assert_awaited_once()
    args, _ = secret_store.set_secret.await_args
    ref_arg, value_arg = args[0], args[1]
    assert ref_arg == resp.api_key_ref
    assert value_arg == _SECRET_VALUE
    assert ref_arg.startswith("cred_tenant-42_openrouter_")


@pytest.mark.asyncio
async def test_register_response_never_contains_the_value() -> None:
    secret_store = AsyncMock()
    event_bus = AsyncMock(spec=EventBusKafka)
    req = _make_request()

    resp = await register_inference_credential(
        req, tenant_id="tenant-42", secret_store=secret_store, event_bus=event_bus
    )

    assert isinstance(resp, ModelInferenceCredentialResponse)
    wire = resp.model_dump_json()
    assert _SECRET_VALUE not in wire
    assert "key_value" not in wire


@pytest.mark.asyncio
async def test_register_publishes_credential_registered_event_without_value() -> None:
    secret_store = AsyncMock()
    event_bus = AsyncMock(spec=EventBusKafka)
    req = _make_request()

    resp = await register_inference_credential(
        req, tenant_id="tenant-42", secret_store=secret_store, event_bus=event_bus
    )

    event_bus.publish_envelope.assert_awaited_once()
    args, kwargs = event_bus.publish_envelope.await_args
    envelope, topic = args[0], args[1]
    assert topic == CREDENTIAL_REGISTERED_TOPIC_V1
    assert isinstance(envelope, ModelEventEnvelope)
    assert kwargs["key"] == resp.api_key_ref.encode("utf-8")

    payload = envelope.payload
    assert isinstance(payload, ModelCredentialRegisteredEvent)
    assert payload.tenant_id == "tenant-42"
    assert payload.provider == "openrouter"
    assert payload.name == "my-openrouter-key"
    assert payload.api_key_ref == resp.api_key_ref

    # Full wire serialization — the value must be structurally absent, not
    # merely omitted by convention.
    wire = json.loads(envelope.model_dump_json())
    wire_str = json.dumps(wire)
    assert _SECRET_VALUE not in wire_str
    assert "key_value" not in wire_str
    assert "value" not in wire["payload"]


@pytest.mark.asyncio
async def test_register_never_logs_the_secret_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log-scrubbing assertion required by OMN-16316 AC5: the key value must
    never appear in any log record emitted during a register call."""
    secret_store = AsyncMock()
    event_bus = AsyncMock(spec=EventBusKafka)
    req = _make_request()

    with caplog.at_level(logging.DEBUG):
        await register_inference_credential(
            req, tenant_id="tenant-42", secret_store=secret_store, event_bus=event_bus
        )

    for record in caplog.records:
        assert _SECRET_VALUE not in record.getMessage()
        assert _SECRET_VALUE not in repr(record.__dict__)


@pytest.mark.asyncio
async def test_owned_event_bus_lifecycle_uses_start_then_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnimarket.projection.credential_publisher as cp

    owned_bus = AsyncMock(spec=EventBusKafka)
    secret_store = AsyncMock()
    monkeypatch.setattr(cp, "_build_event_bus", lambda: owned_bus)
    req = _make_request()

    await register_inference_credential(req, tenant_id="t1", secret_store=secret_store)

    owned_bus.start.assert_awaited_once()
    owned_bus.publish_envelope.assert_awaited_once()
    owned_bus.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_broker_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnimarket.projection.credential_publisher as cp

    def _raise() -> object:
        raise RuntimeError("no broker configured")

    monkeypatch.setattr(cp, "_build_event_bus", _raise)
    secret_store = AsyncMock()
    req = _make_request()

    with pytest.raises(RuntimeError):
        await register_inference_credential(
            req, tenant_id="t1", secret_store=secret_store
        )


@pytest.mark.asyncio
async def test_revoke_publishes_credential_revoked_and_never_calls_delete_secret() -> (
    None
):
    event_bus = AsyncMock(spec=EventBusKafka)

    resp = await revoke_inference_credential(
        "cred_t1_openrouter_abc", tenant_id="t1", event_bus=event_bus
    )

    assert isinstance(resp, ModelInferenceCredentialRevokeResponse)
    assert resp.status == "revocation-published"
    assert resp.api_key_ref == "cred_t1_openrouter_abc"

    event_bus.publish_envelope.assert_awaited_once()
    args, _kwargs = event_bus.publish_envelope.await_args
    envelope, topic = args[0], args[1]
    assert topic == CREDENTIAL_REVOKED_TOPIC_V1
    payload = envelope.payload
    assert isinstance(payload, ModelCredentialRevokedEvent)
    assert payload.tenant_id == "t1"
    assert payload.api_key_ref == "cred_t1_openrouter_abc"
    # No delete_secret surface exists on this publisher's dependency set at
    # all — asserted structurally: revoke_inference_credential takes no
    # secret_store argument, so it cannot call delete_secret even by mistake.
    import inspect

    sig = inspect.signature(revoke_inference_credential)
    assert "secret_store" not in sig.parameters
