# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Consumer configuration model for intent event consumer.

Migrated from singular topic suffix fields to standard event_bus.subscribe_topics
list format per OMN-1746 for EventBusSubcontractWiring compatibility.
"""

from pydantic import BaseModel, ConfigDict, Field


# omnimemory-model-exempt: handler config
class ModelIntentEventConsumerConfig(BaseModel):
    """Configuration for intent event consumer.

    Topic configuration uses list-based fields matching the standard
    ``event_bus.subscribe_topics`` contract format. This enables
    declarative wiring via ``EventBusSubcontractWiring`` instead of
    manual ``subscribe_callback`` injection.

    Note: consumer_group is NOT configured here. It is derived from
    ModelNodeIdentity via compute_consumer_group_id() per ADR.

    Note: Topic suffix defaults MUST match the ``event_bus.subscribe_topics``,
    ``event_bus.publish_topics``, and ``event_bus.dlq_topics`` declared in this
    node's contract.yaml. The contract is the source of truth for topic
    declarations.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    # Topic configuration (suffixes only - env prefix added at runtime)
    # Uses list format matching event_bus.subscribe_topics contract standard
    subscribe_topics: list[str] = Field(
        default=["onex.evt.omniintelligence.intent-classified.v1"],
        description="Topic suffixes to subscribe to (env prefix added at runtime)",
    )
    publish_topics: list[str] = Field(
        default=["onex.evt.omnimemory.intent-stored.v1"],
        description="Topic suffixes to publish to (env prefix added at runtime)",
    )
    dlq_topics: list[str] = Field(
        default=["onex.evt.omniintelligence.intent-classified.v1.dlq"],
        description="Dead letter queue topic suffixes",
    )

    # Circuit breaker configuration
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        description="Failures before circuit opens",
    )
    circuit_breaker_recovery_timeout_seconds: int = Field(
        default=60,
        ge=1,
        description="Seconds before circuit half-opens",
    )

    # Health check configuration
    staleness_threshold_seconds: float = Field(
        default=300.0,
        ge=0,
        description="Seconds without consumption before marked stale",
    )

    # Retry configuration
    retry_max_attempts: int = Field(
        default=3,
        ge=0,
        description="Max retries before DLQ (0 = no retries)",
    )
    retry_backoff_base_seconds: float = Field(
        default=1.0,
        ge=0.1,
        description="Base backoff seconds (exponential: base * 2^attempt)",
    )
