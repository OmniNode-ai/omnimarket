# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The BYOK/generation publishers must carry the broker's transport auth (OMN-17372).

Observed on onex-dev 2026-09-06T19:46:49Z: every
``POST /v1/tenants/me/inference-credentials`` returned
``503 {"detail":"failed to register inference credential"}``. The traceback
was NOT the provider-catalogue validator -- it was
``credential_publisher.register_inference_credential`` ->
``await bus.start()`` ->
``aiokafka.errors.KafkaConnectionError: Unable to bootstrap from
[('b-2...kafka.us-east-1.amazonaws.com', 9098), ('b-1...', 9098)]``,
surfaced as ``[ONEX_CORE_205_SERVICE_UNAVAILABLE]``.

Cause: ``_build_event_bus`` constructed ``ModelKafkaEventBusConfig`` with
``bootstrap_servers`` ALONE. That model defaults ``security_protocol`` to
``PLAINTEXT`` and ``sasl_mechanism`` to ``None``, so the publisher opened a
plaintext connection to an MSK IAM-only listener (9098), which the broker
closes. The gateway process's OTHER Kafka leg -- the kafka-python
``KafkaWorkflowPublisher`` -- reads the same env through its own client config
and works, which is why workflow submission succeeded in the same process and
in the same second that credential registration failed.

The environment already carries the answer (``KAFKA_SECURITY_PROTOCOL=SASL_SSL``,
``KAFKA_SASL_MECHANISM=AWS_MSK_IAM``, ``KAFKA_MSK_REGION`` are all set on the
onex-api Deployment); the config model already knows how to read it via
``apply_environment_overrides()``, which is what the emit-daemon and
event-emit-effect publishers already call. These publishers simply never did.

This is a transport-configuration test, not a broker test: it asserts the
constructed config, so it needs no live Kafka.
"""

from __future__ import annotations

import pytest

from omnimarket.projection import credential_publisher, generation_publisher

_MSK_ENV = {
    "KAFKA_BOOTSTRAP_SERVERS": "b-1.example.kafka.us-east-1.amazonaws.com:9098",
    "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
    "KAFKA_SASL_MECHANISM": "AWS_MSK_IAM",
    "KAFKA_MSK_REGION": "us-east-1",
}


@pytest.fixture
def msk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _MSK_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.unit
def test_credential_bus_carries_sasl_transport_from_env(msk_env: None) -> None:
    """The BYOK intake publisher must not open PLAINTEXT against an IAM listener."""
    bus = credential_publisher._build_event_bus()
    config = bus.config  # type: ignore[attr-defined]
    assert config.security_protocol == "SASL_SSL", (
        "BYOK credential intake built a "
        f"{config.security_protocol} bus against an MSK IAM listener -- this is "
        "the ONEX_CORE_205_SERVICE_UNAVAILABLE -> HTTP 503 seen on onex-dev"
    )
    assert config.sasl_mechanism == "AWS_MSK_IAM"
    assert config.msk_region == "us-east-1"


@pytest.mark.unit
def test_generation_bus_carries_sasl_transport_from_env(msk_env: None) -> None:
    """The generation publisher shares the seam and must resolve it the same way."""
    bus = generation_publisher._build_event_bus()
    config = bus.config  # type: ignore[attr-defined]
    assert config.security_protocol == "SASL_SSL"
    assert config.sasl_mechanism == "AWS_MSK_IAM"
    assert config.msk_region == "us-east-1"


@pytest.mark.unit
def test_plaintext_env_still_yields_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: with no SASL env the local-dev PLAINTEXT shape is unchanged.

    Without this the two assertions above would also pass on a build that
    hardcoded SASL_SSL, which would break every local/compose lane.
    """
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
    monkeypatch.delenv("KAFKA_SECURITY_PROTOCOL", raising=False)
    monkeypatch.delenv("KAFKA_SASL_MECHANISM", raising=False)
    bus = credential_publisher._build_event_bus()
    config = bus.config  # type: ignore[attr-defined]
    assert config.security_protocol == "PLAINTEXT"
    assert config.sasl_mechanism is None
