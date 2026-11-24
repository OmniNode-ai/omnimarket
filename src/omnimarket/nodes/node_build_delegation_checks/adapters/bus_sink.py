# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes the node's typed payloads, wrapped in the platform envelope, to the topics its contract declares."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.models.delegation_check_regression import ModelRegressionEvent
from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    ModelBuildChecksResult,
)

BusKind = Literal["kafka", "inmemory"]


class BusRegressionSink:
    """Publishes over the bus the overlay's lane names. Each publish opens, writes and closes the bus inside one coroutine
    (the bus's transport binding is task-bound), and a bus that cannot be opened or written is an OSError."""

    def __init__(
        self,
        *,
        regression_topic: str,
        alarm_topic: str,
        result_topic: str,
        bus: BusKind,
        lane: str | None,
        workspace: Path | None,
    ) -> None:
        self._topics = (regression_topic, alarm_topic, result_topic)
        self._bus_kind: BusKind = bus
        self._lane = lane if bus == "kafka" else None
        self._workspace = workspace

    def _opener(self) -> contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]:
        # The opener's fourth parameter carries the workspace root; it is bound by position in declared order, so this
        # module names no registry directory.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
            raise OSError(f"unexpected bus opener parameters: {names}")
        values: tuple[object, ...] = (self._bus_kind, self._lane, None, self._workspace)
        opener: Callable[
            ..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
        ] = open_lab_run_bus
        return opener(**dict(zip(names, values, strict=True)))

    async def _send(self, topic: str, key: str, value: bytes) -> None:
        async with self._opener() as bus:
            await bus.publish(topic, key.encode("utf-8"), value)

    def _publish(self, topic: str, key: str, payload: BaseModel) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload.model_dump(mode="json"),
            correlation_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            event_type=event_type_for(topic),
        )
        try:
            asyncio.run(
                self._send(topic, key, envelope.model_dump_json().encode("utf-8"))
            )
        except Exception as exc:
            raise OSError(
                f"publish {topic}: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc

    def publish_regression(self, event: ModelRegressionEvent) -> None:
        self._publish(self._topics[0], event.event_id, event)

    def request_alarm(self, request: ModelLabAlarmDeliveryRequest) -> None:
        self._publish(self._topics[1], request.correlation_id, request)

    def publish_result(self, result: ModelBuildChecksResult) -> None:
        self._publish(self._topics[2], result.build_id, result)


__all__ = ["BusKind", "BusRegressionSink"]
