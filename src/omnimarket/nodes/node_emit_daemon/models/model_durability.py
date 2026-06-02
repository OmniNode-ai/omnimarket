# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Per-topic durability tier model for the emit daemon.

Durability is a declared per-topic property in the event registry. The queue
routes each fan-out target by its tier:

    DUTY_CRITICAL -> append-only durable outbox. Acked replay outbox -> Kafka,
        truncate-on-ack, NEVER drop. On outbox-storage-full the caller is
        signalled with explicit backpressure (DurableOutboxFullError) and the
        emit fails fast -- there is no silent drop.
    TELEMETRY -> bounded in-memory queue + disk spool with drop-oldest on
        overflow. Loss is correct by design for high-volume metrics.

Tier is NOT hardcoded branching: each fan-out rule in ``registries/topics.yaml``
declares a ``tier`` and the registry fails fast on any rule with no tier.
"""

from __future__ import annotations

from enum import StrEnum


class EnumDurabilityTier(StrEnum):
    """Per-topic durability tier.

    DUTY_CRITICAL: commands / evidence that must never be dropped. Routed
        through the append-only durable outbox with truncate-on-ack semantics.
    TELEMETRY: high-volume observability events where bounded loss is the
        correct behavior under sustained backpressure.
    """

    DUTY_CRITICAL = "duty_critical"
    TELEMETRY = "telemetry"


class DurableOutboxFullError(RuntimeError):
    """Raised when a duty-critical event cannot be persisted to the outbox.

    The outbox is full (by message count or bytes) and dropping is prohibited
    for duty-critical events. The emit path surfaces this to the caller as
    explicit backpressure rather than silently dropping the event.
    """


__all__: list[str] = ["DurableOutboxFullError", "EnumDurabilityTier"]
