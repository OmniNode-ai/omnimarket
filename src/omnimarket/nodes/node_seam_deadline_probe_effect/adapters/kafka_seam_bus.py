# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seam bus: one command on the lane's broker and its correlated result, read by a deadline.

The broker address and login come from the lane declaration, resolved the way the dev-lane clients
resolve them; nothing is read from an ambient broker variable. The reply consumer joins a fresh
group under the seam's granted prefix and holds its offsets at the log end before the command is
sent, so a result that lands quickly is not missed. No offset is committed and no topic is created.
The answer's content never leaves this module: only whether it came, how long it took, and whether
it carried an error.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    ModelSeamAnswer,
    ModelSeamDemand,
)

if TYPE_CHECKING:
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig


def _event_type(topic: str) -> str:
    parts = topic.split(".")
    return f"{parts[2]}.{parts[3]}" if len(parts) >= 5 else topic


class KafkaSeamBus:
    """Demand over the broker of ``lane``."""

    def __init__(self, lane: str) -> None:
        self._lane = lane

    def _config(self) -> ModelKafkaEventBusConfig:
        # Imported here: the in-memory backend must not need a Kafka client.
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
        from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

        selection, transport = resolve_lane_binding(self._lane)
        config = ModelKafkaEventBusConfig.default()
        overrides: dict[str, object] = {
            "bootstrap_servers": selection.bootstrap_servers,
            "security_protocol": selection.security_protocol,
            "sasl_mechanism": selection.sasl_mechanism,
        }
        if transport is not None:
            overrides.update(transport.as_client_config_overrides())
        return config.model_copy(update=overrides)

    def demand(self, demand: ModelSeamDemand) -> ModelSeamAnswer:
        try:
            return asyncio.run(self._demand(demand))
        except (
            Exception
        ) as exc:  # a transport that cannot open or read is bus-unreadable
            return ModelSeamAnswer(
                crossing=EnumSeamCrossing.BUS_UNREADABLE,
                detail=reason_token(type(exc).__name__),
            )

    async def _demand(self, demand: ModelSeamDemand) -> ModelSeamAnswer:
        from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
        from omnibase_infra.enums import EnumKafkaAcks
        from omnibase_infra.event_bus.kafka_transport import KafkaTransport

        config = self._config()
        correlation = UUID(demand.correlation_id)
        envelope = ModelEventEnvelope[dict[str, object]](
            payload={**demand.payload, "correlation_id": str(correlation)},
            correlation_id=correlation,
            event_type=_event_type(demand.command_topic),
            tenant_id=None,
        )
        transport = KafkaTransport(
            config=config.model_copy(
                update={"acks": EnumKafkaAcks.ALL, "enable_idempotence": False}
            ),
            group=demand.consumer_group_prefix + uuid4().hex,
            topics=(demand.result_topic,),
            auto_offset_reset="latest",
        )
        try:
            await transport.start()
            started = time.monotonic()
            await transport.send(
                demand.command_topic,
                key=str(correlation).encode("utf-8"),
                value=envelope.model_dump_json().encode("utf-8"),
                headers={},
            )
            deadline = started + demand.deadline_seconds
            while (left := deadline - time.monotonic()) > 0:
                messages = await transport.poll(
                    max_messages=10, timeout_ms=max(1, min(1000, int(left * 1000)))
                )
                for message in messages:
                    answer = _correlated_answer(message.value, str(correlation))
                    if answer is None:
                        continue
                    elapsed = int((time.monotonic() - started) * 1000)
                    if elapsed > demand.deadline_seconds * 1000:
                        return ModelSeamAnswer(
                            crossing=EnumSeamCrossing.NO_RESULT_BY_DEADLINE,
                            elapsed_ms=elapsed,
                            detail="late",
                        )
                    if any(answer.get(key) for key in demand.error_keys):
                        return ModelSeamAnswer(
                            crossing=EnumSeamCrossing.RESULT_ERROR, elapsed_ms=elapsed
                        )
                    return ModelSeamAnswer(
                        crossing=EnumSeamCrossing.RESULT, elapsed_ms=elapsed
                    )
            return ModelSeamAnswer(crossing=EnumSeamCrossing.NO_RESULT_BY_DEADLINE)
        finally:
            await transport.close()


def _correlated_answer(
    value: bytes | None, correlation: str
) -> dict[str, object] | None:
    """The answer payload of an envelope carrying ``correlation``, or None for any other record."""
    try:
        raw = json.loads(value or b"")
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("correlation_id") != correlation:
        return None
    payload = raw.get("payload")
    return payload if isinstance(payload, dict) else {}


__all__ = ["KafkaSeamBus"]
