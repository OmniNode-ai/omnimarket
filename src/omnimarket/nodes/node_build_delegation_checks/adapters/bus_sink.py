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
from types import TracebackType
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
    """One bus opened on first use and closed after the run; a bus that cannot be opened or written is an OSError."""

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
        self._loop = asyncio.new_event_loop()
        self._cm: contextlib.AbstractAsyncContextManager[ProtocolLabRunBus] | None = (
            None
        )
        self._bus: ProtocolLabRunBus | None = None

    def _open(self) -> ProtocolLabRunBus:
        if self._bus is None:
            try:
                # The opener's fourth parameter carries the workspace root; it is bound by position in declared order, so
                # this module names no registry directory.
                names = tuple(inspect.signature(open_lab_run_bus).parameters)
                if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
                    raise OSError(f"unexpected bus opener parameters: {names}")
                values: tuple[object, ...] = (
                    self._bus_kind,
                    self._lane,
                    None,
                    self._workspace,
                )
                opener: Callable[
                    ..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
                ] = open_lab_run_bus
                self._cm = opener(**dict(zip(names, values, strict=True)))
                self._bus = self._loop.run_until_complete(self._cm.__aenter__())
            except Exception as exc:
                raise OSError(
                    f"bus open: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
        return self._bus

    def _publish(self, topic: str, key: str, payload: BaseModel) -> None:
        bus = self._open()
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload.model_dump(mode="json"),
            correlation_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            event_type=event_type_for(topic),
        )
        try:
            self._loop.run_until_complete(
                bus.publish(
                    topic,
                    key.encode("utf-8"),
                    envelope.model_dump_json().encode("utf-8"),
                )
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

    def close(self) -> None:
        try:
            if self._cm is not None:
                self._loop.run_until_complete(self._cm.__aexit__(None, None, None))
        finally:
            self._loop.close()

    def __enter__(self) -> BusRegressionSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["BusKind", "BusRegressionSink"]
